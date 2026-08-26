import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import app.enrichment as enrichment
from app.main import app

client = TestClient(app)

API_KEY = os.environ["MERCHANT_API_KEY"]
AUTH = {"X-API-Key": API_KEY}


# ---------------------------------------------------------------------------
# An isolated dictionary + cache per test, so a test can edit the dictionary
# and observe what the server does about it.
# ---------------------------------------------------------------------------

SEED_DICTIONARY = {
    "_meta": {"note": "test fixture"},
    "merchants": [
        {
            "canonical_name": "Swiggy",
            "aliases": ["swiggy", "swiggy li"],
            "category": "food_delivery",
            "is_essential": False,
            "risk_flag": None,
            "lifestyle_dim": "aspirational",
            "recurring_type": "adhoc",
        },
        {
            "canonical_name": "Jio",
            "aliases": ["jio", "jio recha"],
            "category": "telecom",
            "is_essential": True,
            "risk_flag": None,
            "lifestyle_dim": "essential",
            "recurring_type": "subscription",
        },
    ],
    "gig_payout_sources": {"aliases": ["swiggy", "zomato"]},
}

NYKAA_ENTRY = {
    "canonical_name": "Nykaa",
    "aliases": ["nykaa", "nykaa fashion"],
    "category": "personal_care",
    "is_essential": False,
    "risk_flag": None,
    "lifestyle_dim": "aspirational",
    "recurring_type": "adhoc",
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the resolver at a throwaway dictionary and cache."""
    dict_path = tmp_path / "india_merchants.json"
    dict_path.write_text(json.dumps(SEED_DICTIONARY), encoding="utf-8")
    monkeypatch.setattr(enrichment, "_DICT_PATH", dict_path)
    monkeypatch.setattr(enrichment, "_CACHE_PATH", tmp_path / "cache.db")
    enrichment.reset_server_resolver()
    yield dict_path
    enrichment.reset_server_resolver()


def add_to_dictionary(dict_path, entry):
    data = json.loads(dict_path.read_text(encoding="utf-8"))
    data["merchants"].append(entry)
    dict_path.write_text(json.dumps(data), encoding="utf-8")
    enrichment.reset_server_resolver()


def enrich(names):
    response = client.post("/api/v1/merchants/enrich", json={"merchants": names}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_needs_no_key_and_reports_the_dictionary(sandbox):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["merchants"] == 2
    assert body["dictionary_version"]


def test_unauthorized_enrich():
    assert client.post("/api/v1/merchants/enrich", json={"merchants": ["Swiggy"]}).status_code == 401


def test_wrong_api_key_is_rejected():
    response = client.post(
        "/api/v1/merchants/enrich",
        json={"merchants": ["Swiggy"]},
        headers={"X-API-Key": "not-the-key"},
    )
    assert response.status_code == 401


def test_enrich_fails_closed_when_key_unconfigured(monkeypatch):
    monkeypatch.delenv("MERCHANT_API_KEY", raising=False)
    response = client.post(
        "/api/v1/merchants/enrich", json={"merchants": ["Swiggy"]}, headers={"X-API-Key": "x"}
    )
    assert response.status_code == 503


def test_curation_queue_requires_a_key(sandbox):
    assert client.get("/api/v1/merchants/unresolved").status_code == 401


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_enrich_resolves_known_merchants(sandbox):
    body = enrich(["Swiggy", "Jio", "UnknownMerchantX"])
    results = body["results"]
    assert len(results) == 3

    assert results[0]["canonical_name"] == "Swiggy"
    assert results[0]["category"] == "food_delivery"
    assert results[0]["source"] == "dictionary"

    assert results[1]["is_essential"] is True
    assert results[1]["recurring_type"] == "subscription"

    assert results[2]["category"] == "other"
    assert results[2]["source"] == "unknown"

    assert body["resolved_count"] == 2
    assert body["dictionary_version"] == client.get("/health").json()["dictionary_version"]


def test_truncated_upi_names_still_resolve(sandbox):
    """Real narrations cut merchant names to ~10 characters."""
    results = enrich(["SWIGGY LI", "JIO RECHA"])["results"]
    assert [r["canonical_name"] for r in results] == ["Swiggy", "Jio"]


def test_results_keep_request_order_and_duplicates(sandbox):
    names = ["Jio", "Swiggy", "Jio", "UnknownX", "Swiggy"]
    results = enrich(names)["results"]
    assert [r["canonical_name"] for r in results] == ["Jio", "Swiggy", "Jio", "UnknownX", "Swiggy"]


def test_batch_size_is_capped(sandbox):
    response = client.post(
        "/api/v1/merchants/enrich", json={"merchants": ["x"] * 1001}, headers=AUTH
    )
    assert response.status_code == 422


def test_longest_alias_wins_a_prefix_match(sandbox):
    """A short alias must not swallow a more specific one just because it was
    inserted first — dictionary iteration order is not a ranking."""
    add_to_dictionary(
        sandbox,
        {
            "canonical_name": "Swiggy Instamart",
            "aliases": ["swiggy instamart"],
            "category": "quick_commerce",
            "is_essential": False,
            "risk_flag": None,
            "lifestyle_dim": "aspirational",
            "recurring_type": "adhoc",
        },
    )
    results = enrich(["SWIGGY INSTAMART"])["results"]
    assert results[0]["canonical_name"] == "Swiggy Instamart"


# ---------------------------------------------------------------------------
# The regression this work exists for
# ---------------------------------------------------------------------------


def test_adding_a_merchant_fixes_a_name_that_previously_missed(sandbox):
    """The whole point. `resolve()` reads the cache before the dictionary and
    used to cache misses forever, so a name that failed once was frozen as
    `other` permanently — curating the dictionary changed nothing for exactly
    the merchants users had already hit, and a restart did not help either.
    Cache entries now carry the dictionary version that produced them.
    """
    assert enrich(["NYKAA FASHION"])["results"][0]["category"] == "other"

    add_to_dictionary(sandbox, NYKAA_ENTRY)

    result = enrich(["NYKAA FASHION"])["results"][0]
    assert result["category"] == "personal_care"
    assert result["source"] == "dictionary"


def test_reload_endpoint_picks_up_a_curated_merchant(sandbox):
    """Same fix, without restarting the process."""
    assert enrich(["NYKAA FASHION"])["results"][0]["category"] == "other"

    data = json.loads(sandbox.read_text(encoding="utf-8"))
    data["merchants"].append(NYKAA_ENTRY)
    sandbox.write_text(json.dumps(data), encoding="utf-8")

    before = client.get("/health").json()["dictionary_version"]
    reloaded = client.post("/api/v1/merchants/reload", headers=AUTH).json()
    assert reloaded["dictionary_version"] != before
    assert enrich(["NYKAA FASHION"])["results"][0]["category"] == "personal_care"


def test_a_cached_miss_expires(sandbox, monkeypatch):
    """Even with the dictionary unchanged, a miss must not be permanent — it
    records what we could not resolve today, not a fact about the merchant."""
    assert enrich(["MYSTERY STORE"])["results"][0]["source"] == "unknown"

    resolver = enrichment.get_server_resolver()
    assert resolver._get_from_cache(enrichment.normalize_name("MYSTERY STORE")) is not None

    monkeypatch.setattr(enrichment, "UNKNOWN_TTL_SECONDS", -1)
    assert resolver._get_from_cache(enrichment.normalize_name("MYSTERY STORE")) is None


def test_dictionary_version_changes_when_the_dictionary_changes(sandbox):
    before = client.get("/health").json()["dictionary_version"]
    add_to_dictionary(sandbox, NYKAA_ENTRY)
    assert client.get("/health").json()["dictionary_version"] != before


# ---------------------------------------------------------------------------
# Miss ledger
# ---------------------------------------------------------------------------


def test_unresolved_names_are_recorded_with_counts(sandbox):
    """The count is per *statement*, not per row: a request is deduplicated
    before resolution, so six IRCTC rows in one statement are one lookup and
    one miss. Counting rows would rank a merchant someone pays daily above one
    that many different people cannot resolve, which is the wrong priority for
    a curation queue."""
    enrich(["MYSTERY STORE", "Swiggy", "MYSTERY STORE", "OTHER SHOP"])

    body = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()
    by_name = {i["normalized_name"]: i for i in body["items"]}

    assert "swiggy" not in by_name, "resolved merchants are not gaps"
    assert by_name["mystery store"]["hit_count"] == 1
    assert by_name["other shop"]["hit_count"] == 1
    assert by_name["mystery store"]["sample_raw_name"] == "MYSTERY STORE"

    enrich(["MYSTERY STORE"])
    body = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()
    by_name = {i["normalized_name"]: i for i in body["items"]}
    assert by_name["mystery store"]["hit_count"] == 2


def test_queue_is_ordered_by_demand(sandbox):
    enrich(["RARE SHOP"])
    enrich(["COMMON SHOP"] * 3)
    enrich(["COMMON SHOP"])

    items = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"]
    assert items[0]["normalized_name"] == "common shop"
    # Deduplicated per request, so three copies in one batch count once.
    assert items[0]["hit_count"] == 2


def test_min_count_filters_the_long_tail(sandbox):
    enrich(["SEEN ONCE"])
    enrich(["SEEN TWICE"])
    enrich(["SEEN TWICE"])

    items = client.get(
        "/api/v1/merchants/unresolved", params={"min_count": 2}, headers=AUTH
    ).json()["items"]
    assert [i["normalized_name"] for i in items] == ["seen twice"]


def test_curating_a_merchant_clears_it_from_the_queue(sandbox):
    """The ledger is a to-do list, not a history — once a name resolves it
    should stop asking to be worked on."""
    enrich(["NYKAA FASHION"])
    names = {
        i["normalized_name"]
        for i in client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"]
    }
    assert "nykaa fashion" in names

    add_to_dictionary(sandbox, NYKAA_ENTRY)
    enrich(["NYKAA FASHION"])

    names = {
        i["normalized_name"]
        for i in client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"]
    }
    assert "nykaa fashion" not in names


def test_repeat_misses_keep_updating_last_seen(sandbox):
    enrich(["MYSTERY STORE"])
    first = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"][0]
    time.sleep(0.01)
    enrich(["MYSTERY STORE"])
    second = client.get("/api/v1/merchants/unresolved", headers=AUTH).json()["items"][0]

    assert second["hit_count"] == 2
    assert second["last_seen"] > first["last_seen"]
    assert second["first_seen"] == first["first_seen"]


# ---------------------------------------------------------------------------
# Cache migration
# ---------------------------------------------------------------------------


def test_a_pre_provenance_cache_is_rebuilt_not_trusted(tmp_path, monkeypatch):
    """Old caches have no version column, so their entries cannot be checked
    for staleness. Rebuilding is cheap; serving them is exactly the bug."""
    import sqlite3

    cache_path = tmp_path / "cache.db"
    with sqlite3.connect(cache_path) as conn:
        conn.execute(
            "CREATE TABLE merchant_cache (normalized_name TEXT PRIMARY KEY,"
            " enrichment_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO merchant_cache VALUES (?, ?)",
            ("swiggy", json.dumps({"canonical_name": "STALE", "category": "other",
                                   "is_essential": False, "risk_flag": None,
                                   "lifestyle_dim": "neutral", "recurring_type": "adhoc"})),
        )

    dict_path = tmp_path / "india_merchants.json"
    dict_path.write_text(json.dumps(SEED_DICTIONARY), encoding="utf-8")
    monkeypatch.setattr(enrichment, "_DICT_PATH", dict_path)
    monkeypatch.setattr(enrichment, "_CACHE_PATH", cache_path)
    enrichment.reset_server_resolver()

    assert enrich(["Swiggy"])["results"][0]["canonical_name"] == "Swiggy"
    enrichment.reset_server_resolver()
