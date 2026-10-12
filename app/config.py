"""Server settings.

Read at call time rather than import time so a deployment can rotate a key, and
so tests can set one, without a restart.
"""

from __future__ import annotations

import os


def api_key() -> str:
    """Shared secret the engine sends as X-API-Key."""
    return os.getenv("MERCHANT_API_KEY", "")


# --- proposer ---------------------------------------------------------------


def llm_provider() -> str:
    """gemini | stub. `stub` abstains on everything, which is the correct
    behaviour with no key configured: propose nothing rather than guess."""
    return os.getenv("MERCHANT_LLM_PROVIDER", "gemini" if gemini_api_key() else "stub")


def gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "")


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


def langsearch_api_key() -> str:
    return os.getenv("LANGSEARCH_API_KEY", "")


def llm_timeout() -> float:
    return float(os.getenv("MERCHANT_LLM_TIMEOUT", "20"))


def websearch_timeout() -> float:
    return float(os.getenv("MERCHANT_WEBSEARCH_TIMEOUT", "8"))


# --- promotion thresholds ---------------------------------------------------


def min_hit_count() -> int:
    """How often a name must be asked for before it is worth proposing.

    Doubles as a merchant/person filter, which is the useful part. A person's
    name shows up in one customer's statement; a real merchant shows up across
    many. Requiring repeats across requests keeps most misclassified individuals
    out of the proposal pipeline before anything leaves this server.
    """
    return int(os.getenv("MERCHANT_MIN_HIT_COUNT", "3"))


def min_statement_count() -> int:
    """How many distinct statements a name must appear in before it is proposed.

    The stronger of the two privacy gates: a misclassified person shows up in
    one customer's statements, a merchant across many. Counted per statement
    id sent by the engine, so resubmitting a statement does not raise it.
    Requests without a statement id never count towards it.
    """
    return int(os.getenv("MERCHANT_MIN_STATEMENT_COUNT", "3"))


def promote_threshold() -> float:
    """Confidence needed to auto-promote an ordinary category."""
    return float(os.getenv("MERCHANT_PROMOTE_THRESHOLD", "0.75"))


def high_severity_threshold() -> float:
    """Confidence needed to auto-promote a category that harms the borrower when
    wrong (see taxonomy.HIGH_SEVERITY_CATEGORIES). Corroboration between the
    parametric and web-grounded paths is *also* required for these."""
    return float(os.getenv("MERCHANT_HIGH_SEVERITY_THRESHOLD", "0.90"))


def require_agreement_for_high_severity() -> bool:
    """Set to 0 to hold every category to the same bar."""
    return os.getenv("MERCHANT_REQUIRE_AGREEMENT", "1") not in {"0", "false", "False"}
