from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

RiskFlag = Literal["gambling", "bnpl_lending", "crypto"]
LifestyleDim = Literal["essential", "aspirational", "commitment", "leverage", "risk", "neutral"]
RecurringType = Literal["adhoc", "subscription", "emi_like", "payout_source"]

class MerchantEnrichment(BaseModel):
    canonical_name: str = Field(description="Clean merchant name, e.g. 'Swiggy'")
    category: str = Field(description="One value from docs/taxonomy.md")
    is_essential: bool = Field(description="Counts toward L1 Essential Stability")
    risk_flag: RiskFlag | None = Field(default=None, description="gambling | bnpl_lending | crypto | null")
    lifestyle_dim: LifestyleDim = Field(description="Which L-index this feeds")
    recurring_type: RecurringType = Field(description="Recurrence pattern")

    @classmethod
    def unknown(cls, name: str) -> "MerchantEnrichment":
        return cls(
            canonical_name=name,
            category="other",
            is_essential=False,
            risk_flag=None,
            lifestyle_dim="neutral",
            recurring_type="adhoc",
        )

class EnrichRequest(BaseModel):
    merchants: list[str] = Field(description="Flat list of merchant name strings in order")

class EnrichResponse(BaseModel):
    results: list[MerchantEnrichment] = Field(description="Results in identical order as request")
