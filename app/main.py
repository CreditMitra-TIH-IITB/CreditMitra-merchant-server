from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from app.enrichment import get_server_resolver, reset_server_resolver
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
    results = resolver.resolve_batch(req.merchants)
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
) -> UnresolvedResponse:
    """The curation queue.

    Every name the dictionary could not resolve, with how often it was asked
    for. This is the feedback loop the server previously lacked entirely: a
    miss returned `other`, nothing was recorded, and there was no way to learn
    which merchants were worth adding. Ordered most-asked first so effort goes
    where it buys the most coverage.
    """
    resolver = get_server_resolver()
    rows = resolver.unresolved(limit=limit, min_count=min_count)
    return UnresolvedResponse(
        dictionary_version=resolver.dictionary_version,
        total=len(rows),
        items=[UnresolvedMerchant(**row) for row in rows],
    )


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
