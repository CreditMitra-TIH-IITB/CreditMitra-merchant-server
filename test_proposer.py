"""The curation loop, and the gate that decides what reaches the dictionary.

Promotion is automatic here, so the gate is the only thing standing between a
model's guess and every future borrower's credit score. These tests exist mostly
to hold that gate in place.

No network: a fake provider stands in for the LLM and a fake search for
LangSearch, which is also how you would run a bake-off across models.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.enrichment as enrichment
import app.proposer as proposer
from app.main import app
from app.providers import NO_INFO, Proposal, StubProvider

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}

DICTIONARY = {
    "_meta": {"note": "test fixture"},
    "merchants": [
        {
            "canonical_name": "Swiggy",
            "aliases": ["swiggy"],
            "category": "food_delivery",
            "is_essential": False,
            "risk_flag": None,
            "lifestyle_dim": "aspirational",
            "recurring_type": "adhoc",
        }
    ],
}


class FakeProvider:
    """Returns a scripted proposal per (merchant, grounded?) pair."""

    def __init__(self, grounded: Proposal, parametric: Proposal | None = None) -> None:
        self.name = "fake"
        self._grounded = grounded
        self._parametric = parametric if parametric is not None else grounded
        self.describe_calls: list[tuple[str, str | None]] = []

    def describe(self, merchant: str, evidence: str | None) -> str:
        self.describe_calls.append((merchant, evidence))
        return "a business" if evidence else "a business from memory"

    def classify(self, merchant: str, summary: str) -> Proposal:
        return self._grounded if summary == "a business" else self._parametric


class FakeSearch:
    def __init__(self, result: str | None = "evidence text") -> None:
        self._result = result
        self.enabled = True
        self.queries: list[str] = []

    def search(self, merchant: str) -> str | None:
        self.queries.append(merchant)
        return self._result


def proposal(category, confidence=0.95, name="Proposed", recurring="adhoc"):
    return Proposal(
        category=category,
        canonical_name=name,
        recurring_type=recurring,
        confidence=confidence,
        summary="a business",
        model="fake",
    )


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    dict_path = tmp_path / "india_merchants.json"
    dict_path.write_text(json.dumps(DICTIONARY), encoding="utf-8")
    monkeypatch.setattr(enrichment, "_DICT_PATH", dict_path)
    monkeypatch.setattr(enrichment, "_CACHE_PATH", tmp_path / "cache.db")
    monkeypatch.setenv("MERCHANT_API_KEY", "test-key")
    monkeypatch.setenv("MERCHANT_MIN_HIT_COUNT", "1")
    monkeypatch.setenv("MERCHANT_MIN_STATEMENT_COUNT", "0")
    enrichment.reset_server_resolver()
    yield dict_path
    enrichment.reset_server_resolver()


def seen(name, times=1, statement_id=None):
    """Push a name through enrichment so it lands in the miss ledger."""
    body = {"merchants": [name]}
    if statement_id:
        body["statement_id"] = statement_id
    for _ in range(times):
        client.post("/api/v1/merchants/enrich", json=body, headers=AUTH)


def run(grounded, parametric=None, search="evidence text", limit=25):
    return proposer.run_proposer(
        limit=limit,
        provider=FakeProvider(grounded, parametric),
        websearch=FakeSearch(search),
    )


def dictionary_categories(dict_path):
    data = json.loads(dict_path.read_text(encoding="utf-8"))
    return {m["canonical_name"]: m["category"] for m in data["merchants"]}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_a_confident_proposal_reaches_the_dictionary(sandbox):
    seen("NYKAA FASHION")
    outcomes = run(proposal("personal_care", 0.95, name="Nykaa"))

    assert [o.status for o in outcomes] == ["promoted"]
    assert dictionary_categories(sandbox)["Nykaa"] == "personal_care"


def test_promotion_makes_the_name_resolve(sandbox):
    """The whole loop in one assertion: unresolved -> proposed -> resolving,
    with no human touching anything."""
    seen("NYKAA FASHION")
    assert (
        client.post(
            "/api/v1/merchants/enrich", json={"merchants": ["NYKAA FASHION"]}, headers=AUTH
        ).json()["results"][0]["category"]
        == "other"
    )

    run(proposal("personal_care", 0.95, name="Nykaa"))

    body = client.post(
        "/api/v1/merchants/enrich", json={"merchants": ["NYKAA FASHION"]}, headers=AUTH
    ).json()
    assert body["results"][0]["category"] == "personal_care"
    assert body["results"][0]["source"] == "dictionary"


def test_a_promoted_name_leaves_the_curation_queue(sandbox):
    seen("NYKAA FASHION")
    run(proposal("personal_care", 0.95, name="Nykaa"))
    client.post("/api/v1/merchants/enrich", json={"merchants": ["NYKAA FASHION"]}, headers=AUTH)

    queue = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"]
    assert "nykaa fashion" not in {i["normalized_name"] for i in queue}


def test_names_below_the_hit_threshold_are_not_proposed(sandbox, monkeypatch):
    """Frequency is the cheap merchant/person filter: one person's name appears
    in one statement, a real merchant appears across many."""
    monkeypatch.setenv("MERCHANT_MIN_HIT_COUNT", "3")
    seen("RAMESH KUMAR")
    assert run(proposal("shopping", 0.99)) == []

    seen("RAMESH KUMAR")
    seen("RAMESH KUMAR")
    assert len(run(proposal("shopping", 0.99))) == 1


def test_already_known_merchants_are_never_proposed(sandbox):
    seen("SWIGGY")
    assert run(proposal("gambling", 0.99)) == []


def test_a_promoted_name_is_not_proposed_again(sandbox):
    """Promotion has to clear the queue itself rather than waiting for the next
    enrichment request. Otherwise every subsequent run re-proposes everything
    already promoted — paying for the search and both model calls again, and
    overwriting the audit record of the promotion with the new result."""
    seen("NYKAA FASHION")
    assert len(run(proposal("personal_care", 0.95, name="Nykaa"))) == 1

    assert run(proposal("personal_care", 0.95, name="Nykaa")) == []

    audit = client.get("/api/v1/merchants/candidates", headers=AUTH).json()["items"]
    assert [i["status"] for i in audit] == ["promoted"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_low_confidence_is_held_not_promoted(sandbox):
    seen("MYSTERY STORE")
    outcomes = run(proposal("shopping", 0.40))

    assert outcomes[0].status == "held"
    assert "confidence" in outcomes[0].reason
    assert "Proposed" not in dictionary_categories(sandbox)


def test_abstention_is_held(sandbox):
    seen("MYSTERY STORE")
    outcomes = run(proposal(None, 0.0))

    assert outcomes[0].status == "held"
    assert outcomes[0].reason == "model abstained"


def test_a_high_severity_label_needs_both_paths_to_agree(sandbox):
    """A false gambling tag drives L6 to zero and lands the person in the
    Gambler archetype. Confidence alone is not enough to apply one."""
    seen("SOME APP")
    outcomes = run(proposal("gambling", 0.99), parametric=proposal("shopping", 0.99))

    assert outcomes[0].status == "held"
    assert "disagreed" in outcomes[0].reason
    assert "gambling" not in dictionary_categories(sandbox).values()


def test_a_high_severity_label_promotes_when_both_paths_agree(sandbox):
    seen("DREAM11 CLONE")
    outcomes = run(proposal("gambling", 0.95), parametric=proposal("gambling", 0.95))

    assert outcomes[0].status == "promoted"
    assert outcomes[0].agreement is True


def test_a_high_severity_label_needs_more_confidence_than_an_ordinary_one(sandbox):
    """0.80 promotes a shopping label and holds a lending one."""
    seen("SHOP A")
    assert run(proposal("shopping", 0.80))[0].status == "promoted"

    seen("LENDER B")
    outcomes = run(proposal("bnpl_lending", 0.80), parametric=proposal("bnpl_lending", 0.80))
    assert outcomes[0].status == "held"
    assert "high-severity" in outcomes[0].reason


def test_flattening_the_agreement_rule_is_one_setting(sandbox, monkeypatch):
    monkeypatch.setenv("MERCHANT_REQUIRE_AGREEMENT", "0")
    seen("SOME APP")
    outcomes = run(proposal("gambling", 0.99), parametric=proposal("shopping", 0.50))
    assert outcomes[0].status == "promoted"


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_both_a_grounded_and_a_parametric_opinion_are_taken(sandbox):
    seen("MYSTERY STORE")
    provider = FakeProvider(proposal("shopping", 0.9), proposal("shopping", 0.9))
    proposer.run_proposer(limit=5, provider=provider, websearch=FakeSearch("evidence"))

    evidences = [evidence for _, evidence in provider.describe_calls]
    assert "evidence" in evidences and None in evidences


def test_the_parametric_opinion_is_used_when_search_finds_nothing(sandbox):
    seen("MYSTERY STORE")
    outcomes = run(proposal(None, 0.0), parametric=proposal("shopping", 0.9), search=None)
    assert outcomes[0].status == "promoted"
    assert outcomes[0].category == "shopping"


def test_derived_fields_come_from_the_taxonomy_not_the_model(sandbox):
    """The model chooses one category; everything the category implies is looked
    up. An internally inconsistent enrichment cannot be expressed."""
    seen("DREAM11 CLONE")
    run(proposal("gambling", 0.95), parametric=proposal("gambling", 0.95))

    entry = next(
        m
        for m in json.loads(sandbox.read_text(encoding="utf-8"))["merchants"]
        if m["category"] == "gambling"
    )
    assert entry["risk_flag"] == "gambling"
    assert entry["lifestyle_dim"] == "risk"
    assert entry["is_essential"] is False


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_every_promotion_records_its_evidence(sandbox):
    seen("NYKAA FASHION")
    run(proposal("personal_care", 0.95, name="Nykaa"))

    entry = next(
        m
        for m in json.loads(sandbox.read_text(encoding="utf-8"))["merchants"]
        if m["canonical_name"] == "Nykaa"
    )
    provenance = entry["provenance"]
    assert provenance["source"] == "auto"
    assert provenance["model"] == "fake"
    assert provenance["confidence"] == 0.95
    assert provenance["evidence"]
    assert provenance["promoted_at"] > 0


def test_candidates_endpoint_shows_held_and_promoted_with_reasons(sandbox):
    seen("GOOD ONE")
    run(proposal("shopping", 0.95))
    seen("BAD ONE")
    run(proposal("shopping", 0.10))

    items = client.get("/api/v1/merchants/candidates", headers=AUTH).json()["items"]
    by_status = {i["status"] for i in items}
    assert by_status == {"promoted", "held"}
    assert all(i["reason"] for i in items)

    held = client.get(
        "/api/v1/merchants/candidates", params={"status": "held"}, headers=AUTH
    ).json()["items"]
    assert {i["status"] for i in held} == {"held"}


def test_an_automatic_promotion_can_be_reversed(sandbox):
    seen("NYKAA FASHION")
    run(proposal("personal_care", 0.95, name="Nykaa"))
    assert "Nykaa" in dictionary_categories(sandbox)

    response = client.post("/api/v1/merchants/reverse/nykaa fashion", headers=AUTH)
    assert response.status_code == 200
    assert "Nykaa" not in dictionary_categories(sandbox)


def test_reversing_never_removes_a_hand_curated_entry(sandbox):
    """An audit undoes what the machine did, not what a person decided."""
    response = client.post("/api/v1/merchants/reverse/swiggy", headers=AUTH)
    assert response.status_code == 404
    assert "Swiggy" in dictionary_categories(sandbox)


def test_reversal_makes_the_name_stop_resolving(sandbox):
    seen("NYKAA FASHION")
    run(proposal("personal_care", 0.95, name="Nykaa"))
    client.post("/api/v1/merchants/reverse/nykaa fashion", headers=AUTH)

    body = client.post(
        "/api/v1/merchants/enrich", json={"merchants": ["NYKAA FASHION"]}, headers=AUTH
    ).json()
    assert body["results"][0]["category"] == "other"


def test_propose_endpoint_requires_a_key(sandbox):
    assert client.post("/api/v1/merchants/propose").status_code == 401


def test_candidates_endpoint_requires_a_key(sandbox):
    assert client.get("/api/v1/merchants/candidates").status_code == 401


# ---------------------------------------------------------------------------
# Default posture
# ---------------------------------------------------------------------------


def test_with_no_model_configured_nothing_is_proposed(sandbox):
    """A server with no API key must add nothing rather than guess from names."""
    seen("MYSTERY STORE")
    outcomes = proposer.run_proposer(
        limit=5, provider=StubProvider(), websearch=FakeSearch(None)
    )
    assert outcomes[0].status == "held"
    assert outcomes[0].category is None


def test_stub_provider_always_abstains():
    stub = StubProvider()
    assert stub.describe("anything", None) == NO_INFO
    assert stub.classify("anything", "a business").abstained


def test_resubmitting_one_statement_cannot_reach_the_statement_bar(sandbox, monkeypatch):
    """The privacy gate: a person's name recurs within one customer's statement.
    However often that one statement is resubmitted, it counts once."""
    monkeypatch.setenv("MERCHANT_MIN_STATEMENT_COUNT", "2")
    seen("RAMESH KUMAR", times=5, statement_id="statement-a")
    assert run(proposal("shopping", 0.99)) == []

    seen("RAMESH KUMAR", statement_id="statement-b")
    assert len(run(proposal("shopping", 0.99))) == 1


def test_requests_without_a_statement_id_never_count_towards_the_bar(sandbox, monkeypatch):
    monkeypatch.setenv("MERCHANT_MIN_STATEMENT_COUNT", "1")
    seen("RAMESH KUMAR", times=10)
    assert run(proposal("shopping", 0.99)) == []


class QuotaSpentProvider(FakeProvider):
    """Answers for the first merchant, then reports its quota spent."""

    def __init__(self, grounded: Proposal) -> None:
        super().__init__(grounded)
        self.exhausted = False

    def describe(self, merchant: str, evidence: str | None) -> str:
        if len(self.describe_calls) >= 2:
            self.exhausted = True
        return super().describe(merchant, evidence)


def test_a_spent_quota_stops_the_run_and_leaves_names_queued(sandbox):
    seen("FIRST SHOP")
    seen("SECOND SHOP")
    seen("THIRD SHOP")

    provider = QuotaSpentProvider(proposal("shopping", 0.99))
    outcomes = proposer.run_proposer(limit=25, provider=provider, websearch=FakeSearch())

    judged = [o for o in outcomes if "quota" not in o.reason]
    stopped = [o for o in outcomes if "quota" in o.reason]
    assert len(judged) == 1 and len(stopped) == 1  # third name never attempted
    assert stopped[0].status == "held"
    queue = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"]
    assert len(queue) == 2  # both unjudged names stay queued for the next run
