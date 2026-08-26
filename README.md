# CreditMitra Merchant Server

> Standalone merchant-enrichment microservice — resolves raw bank-statement
> payee strings into canonical merchants with a category, lifestyle dimension,
> and risk flag.

Extracted from the [CreditMitra Engine](https://github.com/CreditMitra-TIH-IITB/CreditMitra-engine)
so that merchant knowledge can be updated, scaled, and audited independently of
the statement-parsing pipeline (Issue #9).

---

## Why it is separate

The engine sees real bank statements. This service only ever sees a **flat list
of merchant name strings** — no amounts, no dates, no balances, no account
identifiers. Keeping it in its own process makes that privacy boundary explicit
and testable rather than a convention someone has to remember.

```mermaid
flowchart LR
    ENG["CreditMitra Engine"] -- "POST /api/v1/merchants/enrich<br/>['SWIGGY*ORDER', 'JIO RECHARGE']" --> MS["Merchant Server"]
    MS --> CACHE["SQLite cache"]
    MS --> DICT["india_merchants.json"]
    MS -- "canonical name, category,<br/>lifestyle_dim, risk_flag" --> ENG
```

If this service is unreachable, the engine falls back to its own local resolver,
so enrichment degrades rather than fails.

---

## Resolution order

| Step | Source | Notes |
|---|---|---|
| 1 | SQLite cache | Keyed on the normalized name, stamped with the dictionary version |
| 2 | `data/india_merchants.json` | Exact alias match, then longest-prefix match (aliases ≥ 4 chars) |
| 3 | Fallback | `category: other`, `source: unknown`, and a row in the curation queue |

Normalization strips punctuation and common noise tokens (`PVT`, `LTD`,
`PAYMENTS`, `RECHARGE`, `INDIA`, …) before matching, which is what lets
`SWIGGY*ORDER PVT LTD` and `Swiggy` collapse to the same entry. Prefix matching
handles UPI truncation — real narrations cut names to about ten characters, so
`SWIGGY LI` and `FLIPKAR T` have to resolve.

### The cache cannot go stale

Every cached answer carries the **content hash of the dictionary that produced
it**, and misses additionally expire after seven days. Both rules exist because
the cache is consulted *before* the dictionary: without them, a name that failed
to resolve once was frozen as `other` permanently, so adding a merchant changed
nothing for exactly the merchants users had already hit. Restarting did not help
either — only deleting the database did.

Editing the dictionary changes its hash, which retires every answer derived from
the old one. No cache flush, and `POST /api/v1/merchants/reload` picks up an edit
without a restart.

## The curation queue

A miss used to be invisible: the caller got `other`, nothing was recorded, and
there was no way to learn which merchants were worth adding. Unresolved names are
now logged with a frequency count.

```bash
curl -H "X-API-Key: $MERCHANT_API_KEY" \
  "http://localhost:8001/api/v1/merchants/unresolved?min_count=3"
```

```json
{
  "dictionary_version": "da5fffc406c3",
  "total": 2,
  "items": [
    { "normalized_name": "sri balaji stores", "sample_raw_name": "SRI BALAJI STORES",
      "hit_count": 47, "first_seen": 1756200000.0, "last_seen": 1756900000.0 }
  ]
}
```

Counts are **per request, not per row** — a batch is deduplicated before
resolution, so six IRCTC lines in one statement are one lookup and one miss.
Counting rows would rank a merchant one person pays daily above one that many
different people cannot resolve, which is the wrong priority for a work queue.

Once a name resolves it is dropped from the queue, so the list stays a to-do
rather than a history. The intended loop:

```
unresolved name ──► queue (name + frequency) ──► add to dictionary
                                                       │
                                             version changes, stale
                                             answers retire themselves
```

> The queue concentrates exactly the names that do not look like merchants —
> which includes any person misclassified as one upstream. Treat it as reviewed
> data, not a public endpoint, and keep it behind the API key as it is today.

---

## API

### `GET /health`

```json
{ "status": "ok", "service": "merchant-server" }
```

### `POST /api/v1/merchants/enrich`

Requires the `X-API-Key` header. Results come back in the **same order** as the
request, one per input string.

```bash
curl -X POST http://localhost:8001/api/v1/merchants/enrich \
  -H "X-API-Key: $MERCHANT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"merchants": ["SWIGGY*ORDER", "JIO RECHARGE"]}'
```

```json
{
  "results": [
    {
      "canonical_name": "Swiggy",
      "category": "food_delivery",
      "is_essential": false,
      "risk_flag": null,
      "lifestyle_dim": "aspirational",
      "recurring_type": "adhoc"
    }
  ]
}
```

| Status | Meaning |
|---|---|
| `200` | Enriched |
| `401` | Missing or wrong `X-API-Key` |
| `422` | More than 1000 names in one batch |
| `503` | `MERCHANT_API_KEY` is not configured on the server |

Each result carries a `source` (`dictionary` or `unknown`) and the response
carries `dictionary_version` and `resolved_count`. Without provenance a caller
cannot tell a real hit from the safe fallback — both arrive as a well-formed
`MerchantEnrichment` — so resolution quality is unmeasurable and the engine's
`merchant_resolution_rate` counts failures as successes.

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `MERCHANT_API_KEY` | Yes | Shared secret the engine sends as `X-API-Key` |

The endpoint **fails closed**: with no key configured it returns `503` rather
than serving unauthenticated requests. Set the same value as the engine's
`MERCHANT_API_KEY`, and point the engine at this service with
`MERCHANT_ENRICHMENT_URL=http://localhost:8001`.

---

## Running

```bash
pip install -r requirements.txt
MERCHANT_API_KEY=dev-key uvicorn app.main:app --port 8001
```

Docker:

```bash
docker build -t creditmitra-merchant-server .
docker run -p 8001:8001 -e MERCHANT_API_KEY=dev-key creditmitra-merchant-server
```

Tests:

```bash
pip install -r requirements-dev.txt && pytest -q
```

---

## Data handling

`data/enrichment_cache.db` is written at runtime and holds payee strings taken
from real statements, so it is excluded by both `.gitignore` and
`.dockerignore`. Do not commit it or copy it into an image.
