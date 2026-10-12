"""GeminiProvider retries transient API errors instead of reading them as an
abstention (seen live: bursts of 503s held back resolvable merchants)."""

import json

import httpx
import pytest

from app import providers
from app.providers import GeminiProvider


def _response(status, payload=None):
    request = httpx.Request("POST", "https://example.invalid")
    if payload is None:
        return httpx.Response(status, request=request)
    text = json.dumps(payload)
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return httpx.Response(status, json=body, request=request)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)

    def install(*responses):
        queue = list(responses)

        def post(self, url, **kwargs):
            log.append(url)
            return queue.pop(0)

        monkeypatch.setattr(httpx.Client, "post", post)
        return log

    return install


def test_transient_errors_are_retried(calls):
    log = calls(_response(503), _response(429), _response(200, {"summary": "A fashion brand"}))
    assert GeminiProvider(api_key="k", model="m").describe("BEWAKOOF", None) == "A fashion brand"
    assert len(log) == 3


def test_a_missing_model_is_not_retried(calls):
    log = calls(_response(404))
    assert GeminiProvider(api_key="k", model="retired").describe("BEWAKOOF", None) == "No information found"
    assert len(log) == 1


def test_persistent_overload_gives_up_and_abstains(calls):
    log = calls(*[_response(503)] * providers._MAX_ATTEMPTS)
    proposal = GeminiProvider(api_key="k", model="m").classify("BEWAKOOF", "A fashion brand")
    assert proposal.abstained
    assert len(log) == providers._MAX_ATTEMPTS


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("[Errno -2] Name or service not known"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
)
def test_transport_errors_are_retried(monkeypatch, error):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    outcomes = [error, _response(200, {"summary": "A fashion brand"})]

    def post(self, url, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx.Client, "post", post)
    assert GeminiProvider(api_key="k", model="m").describe("BEWAKOOF", None) == "A fashion brand"


def test_a_spent_daily_quota_is_not_retried_and_stops_further_calls(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    request = httpx.Request("POST", "https://example.invalid")
    spent = httpx.Response(
        429,
        json={"error": {"details": [{"@type": "RetryInfo", "retryDelay": "51698s"}]}},
        request=request,
    )
    calls = []

    def post(self, url, **kwargs):
        calls.append(url)
        return spent

    monkeypatch.setattr(httpx.Client, "post", post)
    gemini = GeminiProvider(api_key="k", model="m")

    assert gemini.describe("BEWAKOOF", None) == "No information found"
    assert gemini.exhausted
    assert gemini.describe("FABINDIA", None) == "No information found"
    assert len(calls) == 1  # no retries, and nothing after the quota was spent


def test_a_short_rate_limit_is_still_retried(calls):
    request = httpx.Request("POST", "https://example.invalid")
    brief = httpx.Response(
        429, json={"error": {"details": [{"retryDelay": "2s"}]}}, request=request
    )
    log = calls(brief, _response(200, {"summary": "A fashion brand"}))
    assert GeminiProvider(api_key="k", model="m").describe("BEWAKOOF", None) == "A fashion brand"
    assert len(log) == 2
