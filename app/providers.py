"""Evidence and proposals: web search, and the language models behind a
swappable interface.

WP3 calls for experimenting with multiple LLMs on cost, coverage and speed, so
the provider is an interface rather than a hardcoded vendor. Gemini is the
default because the team already runs it in the Synthetic-Transaction-Generator.

Two prompts, adapted from the engine's tier 3 (Issue #7b) — the design there was
already right and is reused rather than reinvented:

  1. Describe the business, with an explicit escape hatch. A model forced to
     produce a description will produce one, and it will be wrong for exactly
     the obscure merchants this pipeline exists to resolve.
  2. Classify, given the description. The model picks ONE category; everything
     else (is_essential, lifestyle_dim, risk_flag) is looked up from the
     taxonomy, so an internally inconsistent enrichment cannot be expressed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app import config
from app.taxonomy import VALID_CATEGORIES, VALID_RECURRING

logger = logging.getLogger(__name__)

NO_INFO = "No information found"

_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = 2.0


@dataclass(frozen=True)
class Proposal:
    """One model's opinion. `category is None` means it declined to answer,
    which is a first-class outcome and not a failure."""

    category: str | None
    canonical_name: str
    recurring_type: str
    confidence: float
    summary: str
    model: str

    @property
    def abstained(self) -> bool:
        return self.category is None


class LLMProvider(Protocol):
    name: str

    def describe(self, merchant: str, evidence: str | None) -> str:
        """2-3 lines about the business, or NO_INFO."""
        ...

    def classify(self, merchant: str, summary: str) -> Proposal:
        ...


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DESCRIBE_PROMPT = """You are given an Indian merchant name from a UPI bank transaction.
The name is often TRUNCATED or abbreviated ("SWIGGY LI" = Swiggy, "FLIPKAR T" = Flipkart,
"JIO RECHA" = Jio Recharge).

Describe the business in 2-3 lines: what it sells, its industry, its domain.

Rules:
- If web evidence is provided below, rely on it. Otherwise use only what you
  actually know.
- If you do not recognise this merchant, return EXACTLY: "{no_info}"
- Do NOT reconstruct a name from its letters. "OGI12" is not "OGI International
  Limited" unless you genuinely know that company.
- An unrecognised local shop is the expected case, not a failure. Say so.

Merchant name: {name}
{evidence_block}
Return JSON: {{"summary": "<2-3 lines, or '{no_info}'>"}}"""


_CLASSIFY_PROMPT = """Classify an Indian merchant for a credit-assessment system.

You are given the merchant name AND a description of its business. Trust the
description over the truncated name.

Pick EXACTLY one `category` from:
{categories}

`recurring_type`: {recurring}
`canonical_name`: the clean brand name, e.g. "Swiggy".
`confidence`: 0.0-1.0, how sure you are of the category specifically.

Rules:
- If the description is "{no_info}" or empty, return category "other" with
  confidence 0.0. Do NOT guess.
- Be conservative with gambling, bnpl_lending, crypto and loan_emi. A payments
  app is not a lender, and a wallet is not a betting site. Choosing one of these
  wrongly does real harm to the person being assessed, so only use them when the
  description says so plainly.
- `confidence` should reflect the category, not how confident you feel in
  general. Low confidence is a useful answer.

Merchant name: {name}
What we know about it: {summary}"""


def _describe_prompt(name: str, evidence: str | None) -> str:
    block = f"\nWeb evidence:\n{evidence}\n" if evidence else ""
    return _DESCRIBE_PROMPT.format(name=name, evidence_block=block, no_info=NO_INFO)


def _classify_prompt(name: str, summary: str) -> str:
    return _CLASSIFY_PROMPT.format(
        name=name,
        summary=summary,
        categories=", ".join(VALID_CATEGORIES),
        recurring=" | ".join(VALID_RECURRING),
        no_info=NO_INFO,
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class StubProvider:
    """Abstains on everything.

    The default when no API key is configured, and the right default: a server
    with no model should add nothing to the dictionary rather than fall back to
    guessing from the name.
    """

    name = "stub"

    def describe(self, merchant: str, evidence: str | None) -> str:
        return NO_INFO

    def classify(self, merchant: str, summary: str) -> Proposal:
        return Proposal(
            category=None,
            canonical_name=merchant,
            recurring_type="adhoc",
            confidence=0.0,
            summary=NO_INFO,
            model=self.name,
        )


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "canonical_name": {"type": "string"},
        "category": {"type": "string", "enum": list(VALID_CATEGORIES)},
        "recurring_type": {"type": "string", "enum": list(VALID_RECURRING)},
        "confidence": {"type": "number"},
    },
    "required": ["canonical_name", "category", "recurring_type", "confidence"],
}

_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class GeminiProvider:
    """Gemini via the REST API.

    Raw HTTP rather than the SDK: httpx is already a dependency, the call is
    twenty lines, and it does not break when the vendor reorganises its client
    library. `responseSchema` constrains the category to the taxonomy at the
    decode step, so an out-of-vocabulary category cannot come back at all.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else config.gemini_api_key()
        self._model = model or config.gemini_model()
        self.name = f"gemini:{self._model}"

    def _generate(self, prompt: str, schema: dict) -> dict | None:
        if not self._api_key:
            return None
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.0,
            },
        }
        # A failed call reads as an abstention. Without retrying transient
        # errors (429 rate limit, 5xx overload, timeouts — all seen live),
        # an overloaded API silently held back merchants it could have
        # resolved, and broke the two-path agreement that high-severity
        # categories require.
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=config.llm_timeout()) as client:
                    response = client.post(
                        url, headers={"x-goog-api-key": self._api_key}, json=payload
                    )
                if response.status_code in _RETRYABLE and attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS * 2**attempt)
                    continue
                response.raise_for_status()
                body = response.json()
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except httpx.TimeoutException as exc:
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS * 2**attempt)
                    continue
                logger.warning("Gemini call failed: %s", exc)
                return None
            except Exception as exc:
                logger.warning("Gemini call failed: %s", exc)
                return None
        return None

    def describe(self, merchant: str, evidence: str | None) -> str:
        result = self._generate(_describe_prompt(merchant, evidence), _DESCRIBE_SCHEMA)
        if not result:
            return NO_INFO
        return str(result.get("summary") or NO_INFO).strip()

    def classify(self, merchant: str, summary: str) -> Proposal:
        if not summary or summary.strip() == NO_INFO:
            return StubProvider().classify(merchant, summary)

        result = self._generate(_classify_prompt(merchant, summary), _CLASSIFY_SCHEMA)
        if not result:
            return StubProvider().classify(merchant, summary)

        category = result.get("category")
        if category not in VALID_CATEGORIES or category == "other":
            category = None

        recurring = result.get("recurring_type")
        if recurring not in VALID_RECURRING:
            recurring = "adhoc"

        try:
            confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0

        return Proposal(
            category=category,
            canonical_name=str(result.get("canonical_name") or merchant).strip(),
            recurring_type=recurring,
            confidence=confidence if category else 0.0,
            summary=summary,
            model=self.name,
        )


def get_provider() -> LLMProvider:
    if config.llm_provider() == "gemini" and config.gemini_api_key():
        return GeminiProvider()
    return StubProvider()


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


class WebSearch:
    """Merchant name -> web results -> descriptions. Ported from the engine.

    PRIVACY: the outbound query is the merchant name plus a fixed disambiguation
    suffix. Nothing derived from any transaction is ever included — no amount,
    date, balance, narration or counterparty. The suffix exists because a bare
    name like "Westside" collides with unrelated results and starves the model
    of anything about the actual Indian brand.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else config.langsearch_api_key()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def search(self, merchant: str) -> str | None:
        if not self.enabled:
            return None
        try:
            with httpx.Client(timeout=config.websearch_timeout()) as client:
                response = client.post(
                    "https://api.langsearch.com/v1/web-search",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": f"{merchant} India merchant brand",
                        "count": 3,
                        "summary": True,
                        "freshness": "noLimit",
                    },
                )
                response.raise_for_status()
                data = response.json()
            # Results nest under "data", not "SearchData" — the engine hit this
            # and silently got zero results on every call.
            results = (data.get("data", {}).get("webPages", {}).get("value", []))[:3]
            blocks = [
                (item.get("summary") or item.get("snippet") or "")[:500] for item in results
            ]
            blocks = [b for b in blocks if b]
            return "\n\n".join(blocks) if blocks else None
        except Exception as exc:
            logger.info("Web search failed for %r: %s", merchant, exc)
            return None
