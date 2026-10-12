from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from app.enrichment import get_server_resolver, reset_server_resolver
from app.proposer import (
    init_candidate_store,
    list_candidates,
    reverse_promotion,
    run_proposer,
)
from app.schemas import (
    EnrichRequest,
    EnrichResponse,
    UnresolvedMerchant,
    UnresolvedResponse,
)

app = FastAPI(title="CreditMitra Merchant Enrichment Server", version="1.1.0")


def expected_api_key() -> str:
    """Read the shared key at request time so deployments can rotate it
    without a rebuild, and so tests can set it via the environment."""
    return os.getenv("MERCHANT_API_KEY", "")


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    expected = expected_api_key()
    # Fail closed: an unconfigured key must never mean "allow everyone".
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server auth is not configured; set MERCHANT_API_KEY.",
        )
    if not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header",
        )


@app.get("/health")
def health_check() -> dict[str, str | int]:
    resolver = get_server_resolver()
    return {
        "status": "ok",
        "service": "merchant-server",
        "dictionary_version": resolver.dictionary_version,
        "merchants": len(resolver._dictionary),
    }


@app.post(
    "/api/v1/merchants/enrich",
    response_model=EnrichResponse,
    dependencies=[Depends(require_api_key)],
)
def enrich_merchants(req: EnrichRequest) -> EnrichResponse:
    resolver = get_server_resolver()
    results = resolver.resolve_batch(req.merchants, statement_id=req.statement_id)
    return EnrichResponse(
        results=results,
        dictionary_version=resolver.dictionary_version,
        resolved_count=sum(1 for r in results if r.resolved),
    )


@app.get(
    "/api/v1/merchants/unresolved",
    response_model=UnresolvedResponse,
    dependencies=[Depends(require_api_key)],
)
def list_unresolved(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    min_count: Annotated[int, Query(ge=1)] = 1,
    min_statements: Annotated[int, Query(ge=0)] = 0,
) -> UnresolvedResponse:
    """The curation queue.

    Every name the dictionary could not resolve, with how often it was asked
    for. This is the feedback loop the server previously lacked entirely: a
    miss returned `other`, nothing was recorded, and there was no way to learn
    which merchants were worth adding. Ordered most-asked first so effort goes
    where it buys the most coverage.
    """
    resolver = get_server_resolver()
    rows = resolver.unresolved(limit=limit, min_count=min_count, min_statements=min_statements)
    return UnresolvedResponse(
        dictionary_version=resolver.dictionary_version,
        total=len(rows),
        items=[UnresolvedMerchant(**row) for row in rows],
    )


@app.post(
    "/api/v1/merchants/propose",
    dependencies=[Depends(require_api_key)],
)
def propose(limit: Annotated[int, Query(ge=1, le=200)] = 25) -> dict[str, object]:
    """Work the curation queue: search, propose, promote.

    A job, not part of the enrichment path — it makes a web call and two model
    calls per merchant. Run it on a schedule; enrichment keeps answering from
    the dictionary in the meantime.
    """
    outcomes = run_proposer(limit=limit)
    resolver = get_server_resolver()
    return {
        "considered": len(outcomes),
        "promoted": sum(1 for o in outcomes if o.status == "promoted"),
        "held": sum(1 for o in outcomes if o.status == "held"),
        "dictionary_version": resolver.dictionary_version,
        "outcomes": [
            {
                "name": o.raw_name,
                "category": o.category,
                "confidence": round(o.confidence, 3),
                "agreement": o.agreement,
                "status": o.status,
                "reason": o.reason,
                "model": o.model,
            }
            for o in outcomes
        ],
    }


@app.get(
    "/api/v1/merchants/candidates",
    dependencies=[Depends(require_api_key)],
)
def candidates(
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    """The audit trail.

    Promotion is automatic, so this is where a wrong entry gets found. Every
    candidate keeps the evidence, the summary, both models' opinions and the
    reason it was promoted or held — enough to answer "why is this tagged
    gambling" without re-running anything.
    """
    resolver = get_server_resolver()
    init_candidate_store(resolver.candidate_path)
    return {
        "dictionary_version": resolver.dictionary_version,
        "items": list_candidates(resolver.candidate_path, status=status, limit=limit),
    }


@app.post(
    "/api/v1/merchants/reverse/{normalized_name}",
    dependencies=[Depends(require_api_key)],
)
def reverse(normalized_name: str) -> dict[str, object]:
    """Undo an automatic promotion.

    Only removes entries the proposer wrote — a hand-curated merchant is never
    touched by an audit reversal. The dictionary hash changes, so cached answers
    derived from the bad entry retire themselves.
    """
    resolver = get_server_resolver()
    removed = reverse_promotion(
        resolver.dictionary_path, resolver.candidate_path, normalized_name
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No auto-promoted entry with that normalized name",
        )
    return {
        "status": "reversed",
        "normalized_name": normalized_name,
        "dictionary_version": get_server_resolver().dictionary_version,
    }


@app.post(
    "/api/v1/merchants/reload",
    dependencies=[Depends(require_api_key)],
)
def reload_dictionary() -> dict[str, str | int]:
    """Re-read the dictionary from disk after it has been edited.

    Cached answers carry the dictionary version that produced them, so entries
    written against the previous version stop being served automatically — no
    cache flush, and no restart, to pick up newly curated merchants.
    """
    reset_server_resolver()
    resolver = get_server_resolver()
    return {
        "status": "reloaded",
        "dictionary_version": resolver.dictionary_version,
        "merchants": len(resolver._dictionary),
    }
