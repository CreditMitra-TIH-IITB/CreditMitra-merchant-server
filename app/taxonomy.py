"""The category taxonomy, and the fields it determines.

Kept in one place because a proposal from a language model is only as safe as
the constraint applied to it. `category` is the single thing the model chooses;
`is_essential`, `lifestyle_dim` and `risk_flag` are then *looked up*, never
generated. A model that returns `category: "gambling"` with
`lifestyle_dim: "essential"` is not a possibility we have to handle, because it
is not a thing it can express.

Mirrors app/services/merchant_enrichment.py in the engine and docs/taxonomy.md.
"""

from __future__ import annotations

from typing import Literal

LifestyleDim = Literal["essential", "aspirational", "commitment", "leverage", "risk", "neutral"]
RiskFlag = Literal["gambling", "bnpl_lending", "crypto"]

# category: (is_essential, lifestyle_dim, risk_flag)
CATEGORY_TAXONOMY: dict[str, tuple[bool, LifestyleDim, RiskFlag | None]] = {
    "groceries": (True, "essential", None),
    "utilities": (True, "essential", None),
    "telecom": (True, "essential", None),
    "fuel": (True, "essential", None),
    "transport": (True, "essential", None),
    "education": (True, "essential", None),
    "healthcare": (True, "essential", None),
    "food_delivery": (False, "aspirational", None),
    "quick_commerce": (False, "aspirational", None),
    "shopping": (False, "aspirational", None),
    "entertainment": (False, "aspirational", None),
    "travel": (False, "aspirational", None),
    "personal_care": (False, "aspirational", None),
    "dining": (False, "aspirational", None),
    "investments": (False, "commitment", None),
    "insurance": (True, "commitment", None),
    "rent": (True, "commitment", None),
    "loan_emi": (True, "commitment", None),
    "bnpl_lending": (False, "leverage", "bnpl_lending"),
    "gambling": (False, "risk", "gambling"),
    "crypto": (False, "risk", "crypto"),
    "p2p_transfer": (False, "neutral", None),
    "cash_withdrawal": (False, "neutral", None),
    "gig_platform": (False, "neutral", None),
    "bank_charges": (False, "neutral", None),
    "other": (False, "neutral", None),
}

VALID_CATEGORIES = tuple(CATEGORY_TAXONOMY)
VALID_RECURRING = ("adhoc", "subscription", "emi_like", "payout_source")

#: Categories whose label materially harms the borrower when it is wrong.
#:
#: A false `gambling` tag drives L6 to zero and lands the person in the
#: "Gambler" archetype; a false `bnpl_lending` tag does the same through L5.
#: These are also the categories a model is most likely to over-apply, because
#: any payments app can look like a lender from a name alone. They are held to a
#: higher bar before an automatic promotion — a threshold, not a human gate.
HIGH_SEVERITY_CATEGORIES = frozenset({"gambling", "bnpl_lending", "crypto", "loan_emi"})


def is_high_severity(category: str) -> bool:
    return category in HIGH_SEVERITY_CATEGORIES


def derive_fields(category: str) -> tuple[bool, LifestyleDim, RiskFlag | None]:
    """Look up everything the category implies. Raises on an unknown category so
    a malformed proposal fails loudly rather than silently becoming `other`."""
    if category not in CATEGORY_TAXONOMY:
        raise ValueError(f"category not in taxonomy: {category!r}")
    return CATEGORY_TAXONOMY[category]
