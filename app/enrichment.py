from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.schemas import MerchantEnrichment
from app.taxonomy import is_high_severity

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1]
_DICT_PATH = _BASE_DIR / "data" / "india_merchants.json"
_CACHE_PATH = _BASE_DIR / "data" / "enrichment_cache.db"

_CLEAN_RE = re.compile(r"[^\w\s]")
_SUFFIX_RE = re.compile(
    r"\b(PVT|LTD|LIMITED|INC|CORP|LLP|SOLUTIONS|TECHNOLOGIES|SERVICES|RECHARGE"
    r"|PAY|PAYMENTS|INDIA|ONLINE|WEB|STORES)\b",
    re.IGNORECASE,
)

SOURCE_DICTIONARY = "dictionary"
SOURCE_UNKNOWN = "unknown"

#: How long a miss stays cached before the name is retried. A miss is a
#: statement about our coverage today, not a fact about the merchant, so it has
#: to expire — otherwise the first lookup of a name freezes its answer forever.
UNKNOWN_TTL_SECONDS = 7 * 24 * 3600


def normalize_name(name: str) -> str:
    if not name:
        return ""
    text = _CLEAN_RE.sub(" ", name)
    text = _SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _dictionary_version(payload: Any) -> str:
    """Content hash of the merchant list.

    Derived rather than hand-maintained: any edit to the dictionary changes the
    version automatically, so nobody has to remember to bump it and cached
    answers cannot silently outlive the data they came from.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


class MerchantServerResolver:
    def __init__(self) -> None:
        self._dictionary: list[dict[str, Any]] = []
        self._alias_map: dict[str, dict[str, Any]] = {}
        self._gig_aliases: set[str] = set()
        self.dictionary_version = "unknown"
        # Captured at construction so a monkeypatched path stays consistent for
        # the lifetime of this resolver, and so the proposer writes to the same
        # dictionary this instance read.
        self.dictionary_path = _DICT_PATH
        self.cache_path = _CACHE_PATH
        self.candidate_path = _CACHE_PATH.with_name("merchant_candidates.db")
        self._load_dictionary()
        self._init_cache()

    # -- dictionary ---------------------------------------------------------

    def _load_dictionary(self) -> None:
        if not self.dictionary_path.exists():
            logger.warning("Merchant dictionary not found: %s", self.dictionary_path)
            return
        try:
            data = json.loads(self.dictionary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Merchant dictionary unreadable: %s", exc)
            return

        self._dictionary = data.get("merchants", [])
        self.dictionary_version = _dictionary_version(self._dictionary)
        for entry in self._dictionary:
            for alias in entry.get("aliases", []):
                norm = normalize_name(alias)
                if norm:
                    self._alias_map[norm] = entry
        self._gig_aliases = {
            normalize_name(a) for a in data.get("gig_payout_sources", {}).get("aliases", [])
        }
        logger.info(
            "Merchant dictionary v%s: %d merchants, %d aliases",
            self.dictionary_version,
            len(self._dictionary),
            len(self._alias_map),
        )

    # -- storage ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.cache_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # The cache is derived data. If it predates the provenance columns,
            # rebuilding it is cheaper and safer than migrating it — and the old
            # schema is exactly the one that could not expire a miss.
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(merchant_cache)").fetchall()
            }
            if existing and not {"source", "dictionary_version", "cached_at"} <= existing:
                logger.warning("Rebuilding merchant cache: schema predates provenance columns")
                conn.execute("DROP TABLE merchant_cache")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS merchant_cache (
                    normalized_name    TEXT PRIMARY KEY,
                    enrichment_json    TEXT NOT NULL,
                    source             TEXT NOT NULL,
                    dictionary_version TEXT NOT NULL,
                    cached_at          REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS unresolved_merchant (
                    normalized_name TEXT PRIMARY KEY,
                    sample_raw_name TEXT NOT NULL,
                    hit_count       INTEGER NOT NULL DEFAULT 1,
                    first_seen      REAL NOT NULL,
                    last_seen       REAL NOT NULL,
                    statement_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # The queue is curation work, not derived data, so an older ledger is
            # migrated in place rather than rebuilt.
            ledger_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(unresolved_merchant)").fetchall()
            }
            if "statement_count" not in ledger_cols:
                conn.execute(
                    "ALTER TABLE unresolved_merchant"
                    " ADD COLUMN statement_count INTEGER NOT NULL DEFAULT 0"
                )
            # One row per (name, statement) so a statement counts once however
            # often it is resubmitted. Only a hash of the caller's opaque id is kept.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS unresolved_sighting (
                    normalized_name TEXT NOT NULL,
                    statement_key   TEXT NOT NULL,
                    PRIMARY KEY (normalized_name, statement_key)
                )
                """
            )

    # -- cache --------------------------------------------------------------

    def _get_from_cache(self, norm_name: str) -> tuple[MerchantEnrichment, str] | None:
        """Returns (enrichment, source), or None when the entry must not be served.

        An entry is refused when it was written against a different dictionary
        (the answer may have changed) or when it is an expired miss. Without
        those two rules, adding a merchant to the dictionary had no effect on
        any name a user had already looked up.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT enrichment_json, source, dictionary_version, cached_at"
                    " FROM merchant_cache WHERE normalized_name = ?",
                    (norm_name,),
                ).fetchone()
        except Exception as exc:
            logger.debug("Cache read failed for %r: %s", norm_name, exc)
            return None

        if not row:
            return None
        payload, source, version, cached_at = row

        if version != self.dictionary_version:
            return None
        if source == SOURCE_UNKNOWN and (time.time() - cached_at) > UNKNOWN_TTL_SECONDS:
            return None

        try:
            return MerchantEnrichment(**json.loads(payload)), source
        except Exception:
            return None

    def _save_to_cache(self, norm_name: str, enrichment: MerchantEnrichment, source: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO merchant_cache"
                    " (normalized_name, enrichment_json, source, dictionary_version, cached_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        norm_name,
                        enrichment.model_dump_json(),
                        source,
                        self.dictionary_version,
                        time.time(),
                    ),
                )
        except Exception as exc:
            logger.debug("Cache write failed for %r: %s", norm_name, exc)

    # -- miss ledger --------------------------------------------------------

    def _record_miss(self, norm_name: str, raw_name: str, statement_key: str | None) -> None:
        """Every name we could not resolve, with how often it was asked for and
        from how many distinct statements.

        This is the input to curating the dictionary. Without it an unresolved
        merchant is invisible: the caller gets `other`, nothing is logged, and
        nobody ever learns which merchants are worth adding.

        `statement_count` is what gates the proposer, because it is the privacy
        signal: a person's name recurs within one customer's statement, a real
        merchant shows up across many statements. `hit_count` alone could be
        pushed over the bar by resubmitting a single statement.
        """
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO unresolved_merchant
                        (normalized_name, sample_raw_name, hit_count, first_seen, last_seen)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(normalized_name) DO UPDATE SET
                        hit_count = hit_count + 1,
                        last_seen = excluded.last_seen
                    """,
                    (norm_name, raw_name.strip()[:120], now, now),
                )
                if statement_key:
                    new = conn.execute(
                        "INSERT OR IGNORE INTO unresolved_sighting"
                        " (normalized_name, statement_key) VALUES (?, ?)",
                        (norm_name, statement_key),
                    ).rowcount
                    if new:
                        conn.execute(
                            "UPDATE unresolved_merchant SET statement_count = statement_count + 1"
                            " WHERE normalized_name = ?",
                            (norm_name,),
                        )
        except Exception as exc:
            logger.debug("Miss ledger write failed for %r: %s", norm_name, exc)

    def clear_miss(self, norm_name: str) -> None:
        """A name that now resolves is no longer a gap — drop it from the queue
        so the ledger stays a to-do list rather than a history."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM unresolved_merchant WHERE normalized_name = ?", (norm_name,)
                )
                conn.execute(
                    "DELETE FROM unresolved_sighting WHERE normalized_name = ?", (norm_name,)
                )
        except Exception as exc:
            logger.debug("Miss ledger clear failed for %r: %s", norm_name, exc)

    def unresolved(
        self, limit: int = 100, min_count: int = 1, min_statements: int = 0
    ) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT normalized_name, sample_raw_name, hit_count, first_seen, last_seen,"
                    " statement_count FROM unresolved_merchant"
                    " WHERE hit_count >= ? AND statement_count >= ?"
                    " ORDER BY statement_count DESC, hit_count DESC, last_seen DESC LIMIT ?",
                    (min_count, min_statements, limit),
                ).fetchall()
        except Exception as exc:
            logger.warning("Miss ledger read failed: %s", exc)
            return []
        return [
            {
                "normalized_name": r[0],
                "sample_raw_name": r[1],
                "hit_count": r[2],
                "first_seen": r[3],
                "last_seen": r[4],
                "statement_count": r[5],
            }
            for r in rows
        ]

    # -- resolution ---------------------------------------------------------

    def lookup_dict(self, norm_name: str) -> MerchantEnrichment | None:
        entry = self._alias_map.get(norm_name)
        if entry is None:
            # Prefix match handles UPI truncation ("swiggy li" -> "Swiggy").
            # Longest alias wins so a specific entry is not swallowed by a
            # shorter one that happens to be inserted first.
            best: tuple[int, dict[str, Any]] | None = None
            for alias, candidate in self._alias_map.items():
                if len(alias) < 4 or len(norm_name) < 4:
                    continue
                truncation_of_alias = alias.startswith(norm_name)
                extends_alias = norm_name.startswith(alias)
                # A high-severity label must not be reached by a longer name that
                # merely begins with the alias: "navin kumar" is not Navi,
                # "simple mart" is not Simpl. Exact or truncated matches only —
                # same rule as the engine's dictionary tier.
                if is_high_severity(candidate.get("category", "")):
                    extends_alias = False
                if (truncation_of_alias or extends_alias) and (
                    best is None or len(alias) > best[0]
                ):
                    best = (len(alias), candidate)
            if best is None:
                return None
            entry = best[1]

        return MerchantEnrichment(
            canonical_name=entry["canonical_name"],
            category=entry["category"],
            is_essential=entry["is_essential"],
            risk_flag=entry.get("risk_flag"),
            lifestyle_dim=entry["lifestyle_dim"],
            recurring_type=entry["recurring_type"],
            source=SOURCE_DICTIONARY,
        )

    def is_gig_payout_source(self, name: str) -> bool:
        normalized = normalize_name(name)
        if len(normalized) < 4:
            return False
        return any(
            normalized.startswith(a) or a.startswith(normalized)
            for a in self._gig_aliases
            if len(a) >= 4
        )

    def resolve(self, raw_name: str, statement_key: str | None = None) -> MerchantEnrichment:
        norm = normalize_name(raw_name)
        if not norm:
            return MerchantEnrichment.unknown(raw_name)

        cached = self._get_from_cache(norm)
        if cached is not None:
            enrichment, source = cached
            if source == SOURCE_UNKNOWN:
                self._record_miss(norm, raw_name, statement_key)
            return enrichment

        hit = self.lookup_dict(norm)
        if hit is not None:
            self._save_to_cache(norm, hit, SOURCE_DICTIONARY)
            self.clear_miss(norm)
            return hit

        fallback = MerchantEnrichment.unknown(raw_name)
        self._save_to_cache(norm, fallback, SOURCE_UNKNOWN)
        self._record_miss(norm, raw_name, statement_key)
        return fallback

    def resolve_batch(
        self, names: list[str], statement_id: str | None = None
    ) -> list[MerchantEnrichment]:
        """Order-preserving and deduplicated: six IRCTC rows cost one lookup.

        `statement_id` is the caller's opaque id for the statement the batch came
        from; only a hash of it is stored, to count distinct statements.
        """
        statement_key = (
            hashlib.sha256(statement_id.encode("utf-8")).hexdigest()[:24]
            if statement_id
            else None
        )
        unique: dict[str, str] = {}
        for name in names:
            unique.setdefault(normalize_name(name), name)
        resolved = {norm: self.resolve(raw, statement_key) for norm, raw in unique.items()}
        return [resolved[normalize_name(n)] for n in names]


_resolver_instance: MerchantServerResolver | None = None


def get_server_resolver() -> MerchantServerResolver:
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = MerchantServerResolver()
    return _resolver_instance


def reset_server_resolver() -> None:
    """Drop the cached singleton so the dictionary is re-read. Used by tests and
    by the reload endpoint after the dictionary is edited."""
    global _resolver_instance
    _resolver_instance = None
