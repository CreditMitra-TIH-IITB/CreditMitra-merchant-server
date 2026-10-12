"""The curation loop: miss ledger -> proposal -> dictionary.

Runs as a job, never in the request path. Enrichment answers from a dictionary
lookup in microseconds; making a caller wait on a web search and two model calls
would trade the thing the service is good at for the thing it is occasional at.

    for each unresolved name asked for at least MIN_HIT_COUNT times:
        evidence   = web search                       (grounding)
        grounded   = model(name, evidence)            (path 1)
        parametric = model(name, no evidence)         (path 2)
        record candidate with both opinions
        promote if it clears the bar for its severity tier

Two independent paths exist so agreement can be used as a confidence signal.
A model recalling a merchant from its own weights and a model reading live
search results are wrong in different ways; when they land on the same category
that is meaningfully stronger evidence than either alone, and it costs one extra
call on a workload measured in hundreds of merchants.

PROMOTION IS AUTOMATIC. Candidates above threshold are written to the dictionary
without review, and every one is recorded with its evidence, model, confidence
and timestamp so a wrong entry can be found and reversed. High-severity
categories — gambling, bnpl_lending, crypto, loan_emi — need a higher confidence
AND agreement between the two paths, because those are the labels that damage a
borrower when they are wrong, and the ones a model most readily over-applies.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.enrichment import get_server_resolver, normalize_name, reset_server_resolver
from app.providers import LLMProvider, Proposal, WebSearch, get_provider
from app.taxonomy import derive_fields, is_high_severity

logger = logging.getLogger(__name__)

STATUS_PROMOTED = "promoted"
STATUS_HELD = "held"
STATUS_REVERSED = "reversed"


@dataclass(frozen=True)
class CandidateOutcome:
    normalized_name: str
    raw_name: str
    category: str | None
    confidence: float
    agreement: bool
    status: str
    reason: str
    model: str


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_candidate_store(path: Path) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_candidate (
                normalized_name TEXT PRIMARY KEY,
                raw_name        TEXT NOT NULL,
                category        TEXT,
                canonical_name  TEXT NOT NULL,
                recurring_type  TEXT NOT NULL,
                confidence      REAL NOT NULL,
                agreement       INTEGER NOT NULL,
                grounded_category   TEXT,
                parametric_category TEXT,
                evidence        TEXT,
                summary         TEXT,
                model           TEXT NOT NULL,
                status          TEXT NOT NULL,
                reason          TEXT NOT NULL,
                created_at      REAL NOT NULL
            )
            """
        )


def _record(path: Path, row: dict[str, Any]) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO merchant_candidate
              (normalized_name, raw_name, category, canonical_name, recurring_type,
               confidence, agreement, grounded_category, parametric_category,
               evidence, summary, model, status, reason, created_at)
            VALUES (:normalized_name, :raw_name, :category, :canonical_name, :recurring_type,
                    :confidence, :agreement, :grounded_category, :parametric_category,
                    :evidence, :summary, :model, :status, :reason, :created_at)
            """,
            row,
        )


def list_candidates(path: Path, status: str | None = None, limit: int = 100) -> list[dict]:
    query = (
        "SELECT normalized_name, raw_name, category, canonical_name, confidence,"
        " agreement, grounded_category, parametric_category, evidence, summary,"
        " model, status, reason, created_at FROM merchant_candidate"
    )
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()

    keys = [
        "normalized_name", "raw_name", "category", "canonical_name", "confidence",
        "agreement", "grounded_category", "parametric_category", "evidence",
        "summary", "model", "status", "reason", "created_at",
    ]
    return [dict(zip(keys, row, strict=True)) for row in rows]


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def _clears_the_bar(proposal: Proposal, agreement: bool) -> tuple[bool, str]:
    if proposal.category is None:
        return False, "model abstained"

    if is_high_severity(proposal.category):
        threshold = config.high_severity_threshold()
        if config.require_agreement_for_high_severity() and not agreement:
            return False, (
                f"{proposal.category} is high-severity and the grounded and parametric "
                "paths disagreed"
            )
        if proposal.confidence < threshold:
            return False, (
                f"{proposal.category} is high-severity; confidence "
                f"{proposal.confidence:.2f} < {threshold:.2f}"
            )
        return True, f"high-severity category cleared {threshold:.2f} with agreement"

    threshold = config.promote_threshold()
    if proposal.confidence < threshold:
        return False, f"confidence {proposal.confidence:.2f} < {threshold:.2f}"
    return True, f"confidence {proposal.confidence:.2f} >= {threshold:.2f}"


def promote_to_dictionary(
    dictionary_path: Path, normalized: str, proposal: Proposal, evidence: str | None
) -> None:
    """Append an entry and rewrite the dictionary atomically.

    Provenance travels with the entry. Being able to answer "why is this tagged
    gambling, and on what evidence" months later is the difference between an
    auditable system and one that has to be trusted.
    """
    is_essential, lifestyle_dim, risk_flag = derive_fields(proposal.category or "other")

    data = json.loads(dictionary_path.read_text(encoding="utf-8"))
    aliases = sorted({normalized, normalize_name(proposal.canonical_name)} - {""})
    data.setdefault("merchants", []).append(
        {
            "canonical_name": proposal.canonical_name,
            "aliases": aliases,
            "category": proposal.category,
            "is_essential": is_essential,
            "risk_flag": risk_flag,
            "lifestyle_dim": lifestyle_dim,
            "recurring_type": proposal.recurring_type,
            "provenance": {
                "source": "auto",
                "model": proposal.model,
                "confidence": round(proposal.confidence, 3),
                "summary": proposal.summary[:500],
                "evidence": (evidence or "")[:1000],
                "promoted_at": time.time(),
            },
        }
    )

    tmp = dictionary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dictionary_path)


def reverse_promotion(dictionary_path: Path, candidate_path: Path, normalized: str) -> bool:
    """Remove an auto-promoted entry. Hand-curated entries are never touched —
    an audit reverses what the machine did, not what a person decided."""
    data = json.loads(dictionary_path.read_text(encoding="utf-8"))
    merchants = data.get("merchants", [])
    keep = [
        m
        for m in merchants
        if not (
            m.get("provenance", {}).get("source") == "auto"
            and normalized in {normalize_name(a) for a in m.get("aliases", [])}
        )
    ]
    if len(keep) == len(merchants):
        return False

    data["merchants"] = keep
    tmp = dictionary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dictionary_path)

    with _connect(candidate_path) as conn:
        conn.execute(
            "UPDATE merchant_candidate SET status = ?, reason = ? WHERE normalized_name = ?",
            (STATUS_REVERSED, "reversed by audit", normalized),
        )
    reset_server_resolver()
    return True


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def run_proposer(
    limit: int = 25,
    provider: LLMProvider | None = None,
    websearch: WebSearch | None = None,
) -> list[CandidateOutcome]:
    resolver = get_server_resolver()
    dictionary_path = resolver.dictionary_path
    candidate_path = resolver.candidate_path
    init_candidate_store(candidate_path)

    provider = provider or get_provider()
    websearch = websearch if websearch is not None else WebSearch()

    queue = resolver.unresolved(
        limit=limit,
        min_count=config.min_hit_count(),
        min_statements=config.min_statement_count(),
    )
    outcomes: list[CandidateOutcome] = []
    promoted_any = False

    for item in queue:
        normalized, raw = item["normalized_name"], item["sample_raw_name"]

        evidence = websearch.search(raw)
        grounded = provider.classify(raw, provider.describe(raw, evidence))
        parametric = provider.classify(raw, provider.describe(raw, None))

        if getattr(provider, "exhausted", False):
            # The model never judged this name, so record nothing about it: it
            # stays in the queue and is proposed on the next run. Every name
            # after it would fail the same way, so stop here.
            outcomes.append(
                CandidateOutcome(
                    normalized_name=normalized,
                    raw_name=raw,
                    category=None,
                    confidence=0.0,
                    agreement=False,
                    status=STATUS_HELD,
                    reason="LLM quota exhausted; not judged, will be retried on the next run",
                    model=provider.name,
                )
            )
            break

        # Prefer the grounded opinion; fall back to the parametric one when the
        # search returned nothing useful.
        chosen = grounded if not grounded.abstained else parametric
        agreement = (
            grounded.category is not None and grounded.category == parametric.category
        )

        should_promote, reason = _clears_the_bar(chosen, agreement)
        status = STATUS_PROMOTED if should_promote else STATUS_HELD

        if should_promote:
            try:
                promote_to_dictionary(dictionary_path, normalized, chosen, evidence)
                promoted_any = True
                # The name is answered now, so drop it from the queue rather than
                # waiting for the next enrichment request to clear it. Otherwise
                # the following run re-proposes every merchant already promoted —
                # paying for the search and both model calls again, and
                # overwriting the audit record of the promotion with the result.
                resolver.clear_miss(normalized)
            except Exception as exc:
                logger.error("Promotion failed for %r: %s", normalized, exc)
                status, reason = STATUS_HELD, f"promotion failed: {exc}"

        _record(
            candidate_path,
            {
                "normalized_name": normalized,
                "raw_name": raw,
                "category": chosen.category,
                "canonical_name": chosen.canonical_name,
                "recurring_type": chosen.recurring_type,
                "confidence": chosen.confidence,
                "agreement": int(agreement),
                "grounded_category": grounded.category,
                "parametric_category": parametric.category,
                "evidence": (evidence or "")[:2000],
                "summary": chosen.summary[:1000],
                "model": chosen.model,
                "status": status,
                "reason": reason,
                "created_at": time.time(),
            },
        )
        outcomes.append(
            CandidateOutcome(
                normalized_name=normalized,
                raw_name=raw,
                category=chosen.category,
                confidence=chosen.confidence,
                agreement=agreement,
                status=status,
                reason=reason,
                model=chosen.model,
            )
        )

    if promoted_any:
        # Re-read the dictionary. Its content hash changes, which retires every
        # cached `unknown` written against the old one — the loop closes here.
        reset_server_resolver()

    return outcomes
