from __future__ import annotations

import os
import secrets
from typing import Annotated
from fastapi import FastAPI, Header, HTTPException, status
from app.enrichment import get_server_resolver
from app.schemas import EnrichRequest, EnrichResponse

app = FastAPI(title="CreditMitra Merchant Enrichment Server", version="1.0.0")


def expected_api_key() -> str:
    """Read the shared key at request time so deployments can rotate it
    without a rebuild, and so tests can set it via the environment."""
    return os.getenv("MERCHANT_API_KEY", "")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "merchant-server"}

@app.post("/api/v1/merchants/enrich", response_model=EnrichResponse)
def enrich_merchants(
    req: EnrichRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> EnrichResponse:
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

    resolver = get_server_resolver()
    results = [resolver.resolve(name) for name in req.merchants]
    return EnrichResponse(results=results)
