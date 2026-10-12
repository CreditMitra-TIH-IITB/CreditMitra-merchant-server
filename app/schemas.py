from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

RiskFlag = Literal["gambling", "bnpl_lending", "crypto"]
LifestyleDim = Literal["essential", "aspirational", "commitment", "leverage", "risk", "neutral"]
RecurringType = Literal["adhoc", "subscription", "emi_like", "payout_source"]

Source = Literal["dictionary", "unknown"]


class MerchantEnrichment(BaseModel):
    canonical_name: str = Field(description="Clean merchant name, e.g. 'Swiggy'")
    category: str = Field(description="One value from docs/taxonomy.md")
    is_essential: bool = Field(description="Counts toward L1 Essential Stability")
    risk_flag: RiskFlag | None = Field(
        default=None, description="gambling | bnpl_lending | crypto | null"
    )
    lifestyle_dim: LifestyleDim = Field(description="Which L-index this feeds")
    recurring_type: RecurringType = Field(description="Recurrence pattern")
    # Provenance. Without it a caller cannot tell a real dictionary hit from the
    # safe fallback — both arrive as a well-formed MerchantEnrichment — so
    # resolution quality is unmeasurable, and merchant_resolution_rate (which
    # feeds L3 Digital Maturity) silently counts failures as successes.
    source: Source = Field(
        default="unknown", description="Which tier produced this: dictionary | unknown"
    )

    @property
    def resolved(self) -> bool:
        return self.source != "unknown"

    @classmethod
    def unknown(cls, name: str) -> "MerchantEnrichment":
        return cls(
            canonical_name=name,
            category="other",
            is_essential=False,
            risk_flag=None,
            lifestyle_dim="neutral",
            recurring_type="adhoc",
            source="unknown",
        )

#: A statement rarely has more than a few hundred distinct merchants. The cap
#: is here so one malformed caller cannot ask for a million lookups.
MAX_BATCH = 1000


class EnrichRequest(BaseModel):
    merchants: list[str] = Field(
        description="Flat list of merchant name strings in order", max_length=MAX_BATCH
    )
    statement_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Opaque id of the statement this batch came from. Unresolved names are "
            "counted once per statement, so resubmitting one statement cannot push "
            "a name over the proposer's bar. Only a hash of it is stored."
        ),
    )


class EnrichResponse(BaseModel):
    results: list[MerchantEnrichment] = Field(description="Results in identical order as request")
    dictionary_version: str = Field(
        description="Content hash of the merchant dictionary that produced these results"
    )
    resolved_count: int = Field(description="How many of the results came from the dictionary")


class UnresolvedMerchant(BaseModel):
    normalized_name: str
    sample_raw_name: str
    hit_count: int
    first_seen: float
    last_seen: float
    statement_count: int = 0


class UnresolvedResponse(BaseModel):
    """The curation queue: what the dictionary is missing, most-asked first."""

    dictionary_version: str
    total: int
    items: list[UnresolvedMerchant]
