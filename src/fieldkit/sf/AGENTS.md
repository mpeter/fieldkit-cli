# AGENTS.md — Salesforce Client (`sf/`)

Thin synchronous httpx client for the Salesforce REST API. Sole SF data access path since Playwright scraping was retired (D009/D010).

## Authentication boundaries

fieldkit supports an explicitly supplied Salesforce `sid` cookie. It does not
ship an OAuth connected app or attempt to mint credentials for a contributor.

Ask the user for a REST-capable `sid` cookie from an authorized Salesforce
session. Run `fieldkit auth sf` to store it in fieldkit's active
configuration root. `client.py` picks it up automatically. The hostname comes
from `salesforce.org_url` in `accounts.yaml`.

Session lifespan: 8–24 hours. On HTTP 401, `SFAuthError` is raised — callers should prompt the user to re-run `fieldkit auth sf`.

## Query compatibility

fieldkit uses SOSL, direct sObject GET, and UI API relationship routes so it can
operate in Salesforce deployments whose policy blocks SOQL or CPQ child queries.

Allowed patterns:
- **SOSL search** (multi-record): `GET /services/data/v59.0/search/?q=FIND+{term}+IN+NAME+FIELDS+RETURNING+Opportunity(...)`
- **Direct sObject REST GET** (single record): `GET /services/data/v59.0/sobjects/<Object>/<id>?fields=F1,F2`
- **UI API child-relationships** (deal splits): `GET /services/data/v59.0/ui-api/records/<id>/child-relationships/<rel>`
- **UI API related-list-records** (CPQ quote lines, quotes-on-opp): `GET /services/data/v59.0/ui-api/related-list-records/<parentId>/<relatedListId>` — the only working parent→child route for CPQ objects (`SBQQ__Quote__c`, `SBQQ__QuoteLine__c`). `record-ui?childRelationships=true` and `OpportunityLineItems` both fail on CPQ objects (0 keys and HTTP 400 respectively).

Use `IN NAME FIELDS` (not `IN ALL FIELDS`) for opportunity searches — `IN ALL FIELDS` also searches `Next_Steps__c` and causes false-positive account matches when next-steps text mentions another account's name (implementation change).

## Services Filter (R21)

**CONSTRAINT:** Use `Consulting_Total_USD__c > 0 OR Training_Total_USD__c > 0` to identify services opportunities.

Do NOT use `pse__Is_Services_Opportunity__c` — it is `True` on pure subscription renewals (confirmed on <account-slug> accounts during 2026-05-29 audit).

TAM SKUs roll into `Subscription_Total_USD__c`, not `Services_Total_USD__c`. The constant `_SERVICES_FILTER` in `client.py` encodes this correctly — do not change it.

## Exception Hierarchy

- `SFAuthError` — HTTP 401; user must re-auth. Exit code 2.
- `SFNotFoundError` — HTTP 404; record does not exist.
- `SFAPIError` — all other HTTP/network errors. Exit code 3.

Never catch `SFAuthError` silently in the primary data path — it must propagate to `cli_main()` for correct exit code mapping (exit code 2). Exception: supplemental/graceful-degradation helpers (e.g. `_fetch_deal_splits`, `_fetch_quote_lines`) may swallow `SFAuthError` and return an empty result when the supplemental data is non-critical and the header is still useful. These helpers MUST return `[]` / empty and MUST NOT silently suppress — log a `WARNING` for `SFAPIError`; for `SFAuthError`, returning `[]` is acceptable since the primary fetch will also fail and propagate the error.

## Key Implementation Notes

- `fetch_record()` fetches `Opportunity` sObjects. `fetch_sobject()` is the generic form for any object type.
- `fetch_deal_splits()` uses the UI API child-relationships endpoint — the only way to get deal splits without SOQL.
- Retry logic: 3 attempts on HTTP 429/500/502/503/504 with exponential backoff + jitter. `_RETRY_STATUSES` in `client.py` covers gateway errors Salesforce returns under load — do not narrow it.
- `SFDirectClient` is a context manager — always use `with SFDirectClient(...) as client:` to ensure the httpx connection is closed.
- Auth header is `Authorization: Bearer <sid>` (not `Cookie: sid=<sid>`). Both work; Bearer is what `client.py` uses.

## Exception Tach Note (implementation note/implementation note)

`SFAuthError` now inherits from `AuthError` (from `fieldkit.errors`), which `cli_exit.py` catches directly. The old `type(exc).__name__ == "SFAuthError"` workaround was removed. If you add a new auth exception in `sf/`, inherit from `AuthError` — it is automatically routed to EXIT_AUTH (2) without any change to `cli_exit.py`.
