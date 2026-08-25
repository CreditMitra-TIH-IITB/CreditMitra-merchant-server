import os

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

API_KEY = os.environ["MERCHANT_API_KEY"]

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "merchant-server"}

def test_unauthorized_enrich():
    response = client.post("/api/v1/merchants/enrich", json={"merchants": ["Swiggy"]})
    assert response.status_code == 401

def test_wrong_api_key_is_rejected():
    response = client.post(
        "/api/v1/merchants/enrich",
        json={"merchants": ["Swiggy"]},
        headers={"X-API-Key": "not-the-key"},
    )
    assert response.status_code == 401

def test_enrich_fails_closed_when_key_unconfigured(monkeypatch):
    # An unset MERCHANT_API_KEY must lock the endpoint, not open it.
    monkeypatch.delenv("MERCHANT_API_KEY", raising=False)
    response = client.post(
        "/api/v1/merchants/enrich",
        json={"merchants": ["Swiggy"]},
        headers={"X-API-Key": "anything"},
    )
    assert response.status_code == 503

def test_enrich_merchants_success():
    headers = {"X-API-Key": API_KEY}
    payload = {"merchants": ["Swiggy", "Jio", "IRCTC", "UnknownMerchantX"]}
    response = client.post("/api/v1/merchants/enrich", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    results = data["results"]
    assert len(results) == 4

    # Verify Swiggy (Food Delivery / Aspirational)
    assert results[0]["canonical_name"] == "Swiggy"
    assert results[0]["category"] == "food_delivery"
    assert results[0]["lifestyle_dim"] == "aspirational"

    # Verify Jio (Telecom / Essential / Subscription)
    assert results[1]["canonical_name"] == "Jio"
    assert results[1]["category"] == "telecom"
    assert results[1]["is_essential"] is True
    assert results[1]["recurring_type"] == "subscription"

    # Verify Unknown Merchant Fallback
    assert results[3]["canonical_name"] == "UnknownMerchantX"
    assert results[3]["category"] == "other"
    assert results[3]["lifestyle_dim"] == "neutral"
