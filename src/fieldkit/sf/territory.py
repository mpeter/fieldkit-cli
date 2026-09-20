"""implementation note: territory-scoped closed-won pulls layered over :class:`SFDirectClient`.

Territory2 resolution and the closed-won-by-territory query live here rather than
on ``SFDirectClient`` so the core client module stays focused on transport. Each
function takes an ``SFDirectClient`` and drives it through its public
``sosl_search`` plus the sObject REST GET used for the (non-searchable)
Territory2 lookup.
"""

import logging
import re
from collections.abc import Sized
from dataclasses import dataclass

import httpx

from fieldkit.sf.client import (
    _API_VERSION,
    SFAPIError,
    SFDirectClient,
    _parse_json_response,
    _quote_keyword,
)

logger = logging.getLogger(__name__)

# Salesforce record ID shape (15- or 18-char alphanumeric). Territory2Id values
# are interpolated into a SOSL WHERE clause; validating the shape turns a
# hand-edited accounts.yaml typo into a clear skip rather than a confusing SF error.
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15,18}$")
_GSG_ID_RE = re.compile(r"^GSG[0-9]+$")


@dataclass(frozen=True)
class TerritoryResolutionRequest:
    """Configured inputs for resolving one account's Salesforce territory."""

    search_term: str
    expected_developer_name: str
    gsg_id: str | None = None


def _fetch_territory_developer_name(client: SFDirectClient, territory_id: str) -> str | None:
    """Return the ``DeveloperName`` of a Territory2 record, or None on any failure.

    implementation note: Used by :func:`resolve_territory_ids`. Territory2 is not
    SOSL-searchable and SOQL is blocked, so DeveloperName is read via the sObject
    REST GET. Returns None (never raises) on a non-200/parse error so the caller
    can try the next candidate territory.

    Raises:
        SFAuthError: on HTTP 401 (a dead session should stop the whole walk).
    """
    url = f"{client._base_url}/services/data/{_API_VERSION}/sobjects/Territory2/{territory_id}"
    try:
        resp = client._request_with_retry(
            "GET", url, headers=client._auth_headers(), params={"fields": "DeveloperName"}
        )
    except httpx.RequestError as exc:
        logger.debug("_fetch_territory_developer_name: request error for %s: %s", territory_id, exc)
        return None
    if resp.status_code != 200:
        logger.debug("_fetch_territory_developer_name: HTTP %s for %s", resp.status_code, territory_id)
        return None
    try:
        data = _parse_json_response(resp, "Territory2 fetch")
    except SFAPIError:
        return None
    dev_name = data.get("DeveloperName")
    return str(dev_name) if isinstance(dev_name, str) else None


def _account_gsg_filter(slug: str, gsg_id: str | None) -> str:
    """Return the SOSL account-identity filter for a validated GSG ID."""
    if not gsg_id:
        return ""
    if _GSG_ID_RE.fullmatch(gsg_id):
        return f" WHERE Account.GU_Proxy_ID__c = '{gsg_id}'"
    logger.warning(
        "resolve_territory_ids: ignoring malformed sf_gsg_id for %s",
        slug,
    )
    return ""


def resolve_territory_ids(
    client: SFDirectClient,
    accounts: dict[str, TerritoryResolutionRequest],
) -> dict[str, str]:
    """Resolve each account's ``sf_territory`` DeveloperName to a Territory2Id.

    implementation note: Territory2 records are not SOSL-searchable and SOQL is blocked on this
    org, so the only route to a Territory2Id is via an opportunity that already
    carries one. Per account this:

      1. SOSLs the account's search keyword (``IN NAME FIELDS``) to collect the
         distinct ``Territory2Id`` values on its opportunities.
      2. GETs each ``Territory2`` record to read its ``DeveloperName``.
      3. Returns the first Territory2Id whose DeveloperName equals the account's
         expected ``sf_territory`` value.

    Args:
        client: Live Salesforce client.
        accounts: Resolution inputs keyed by account slug. The search term is
            required because a slug (e.g. ``"globex"``) is not itself a
            searchable opportunity name. A valid GSG identifier scopes candidates
            to the corresponding Account records.

    Returns:
        ``{account_slug: territory_id}`` for each slug that resolved. Slugs with no
        matching opportunity/territory are absent from the result; the caller logs
        them. Partial failure never raises.

    Raises:
        SFAuthError: on HTTP 401 (propagated — a dead session must stop here).
    """
    resolved: dict[str, str] = {}
    for slug, request in accounts.items():
        gsg_filter = _account_gsg_filter(slug, request.gsg_id)
        sosl = (
            f"FIND {{{_quote_keyword(request.search_term)}}} IN NAME FIELDS "
            f"RETURNING Opportunity(Id, Territory2Id{gsg_filter} LIMIT 2000)"
        )
        try:
            records = client.sosl_search(sosl)
        except SFAPIError:
            logger.debug("resolve_territory_ids: SOSL failed for %s (%r)", slug, request.search_term)
            continue
        seen_tids: set[str] = set()
        for rec in records:
            tid = rec.get("Territory2Id")
            if not tid or not isinstance(tid, str) or tid in seen_tids:
                continue
            seen_tids.add(tid)
            if _fetch_territory_developer_name(client, tid) == request.expected_developer_name:
                resolved[slug] = tid
                break
        if slug not in resolved:
            logger.warning(
                "resolve_territory_ids: no Territory2 named %r found among %d opp(s) for %s",
                request.expected_developer_name,
                len(records),
                slug,
            )
    return resolved


def fetch_closed_won_by_territory(client: SFDirectClient, account_names: list[str], territory_ids: list[str]) -> float:
    """Return summed ``Consulting_Total_USD__c`` for closed-won FY opps in a territory patch.

    implementation note: All account names are OR-joined into a single SOSL FIND so the
    function issues one remote call regardless of patch size. The territory
    filter in the ``RETURNING`` ``WHERE`` clause prevents cross-territory
    contamination — a customer like a national bank has account records in
    many territories worldwide, and an unscoped name search sums all of them.
    Opportunity Ids are de-duplicated in-process before summing.

    Args:
        client: Live Salesforce client.
        account_names: SOSL search terms (one per patch account).
        territory_ids: ``Territory2Id`` values that define the operator's patch.

    Returns:
        Total closed-won consulting ACV (fiscal-year-to-date), or ``0.0`` when
        nothing matches or no inputs are supplied.

    Raises:
        SFAuthError: on HTTP 401.
        SFAPIError: on other Salesforce API errors or a capped, incomplete result.
    """
    valid_ids = [t for t in territory_ids if _SF_ID_RE.match(t)]
    for bad in sorted(set(territory_ids) - set(valid_ids)):
        logger.warning(
            "fetch_closed_won_by_territory: skipping malformed Territory2Id %r (not a Salesforce record ID)",
            bad,
        )
    if not account_names or not valid_ids:
        return 0.0
    territory_filter = " OR ".join(f"Territory2Id = '{tid}'" for tid in valid_ids)
    search_terms = " OR ".join(_quote_keyword(name) for name in account_names)
    sosl = (
        f"FIND {{{search_terms}}} IN NAME FIELDS "
        f"RETURNING Opportunity(Id, Consulting_Total_USD__c "
        f"WHERE StageName = 'Closed Won' "
        f"AND CloseDate >= THIS_FISCAL_YEAR "
        f"AND Consulting_Total_USD__c > 0 "
        f"AND ({territory_filter}) LIMIT 2000)"
    )
    records = client.sosl_search(sosl)
    _reject_capped_closed_won(records)
    acv_by_id: dict[str, float] = {}
    for rec in records:
        opp_id = rec.get("Id")
        if not opp_id or not isinstance(opp_id, str):
            continue
        acv_by_id[opp_id] = float(rec.get("Consulting_Total_USD__c") or 0.0)
    return sum(acv_by_id.values())


def _reject_capped_closed_won(records: Sized) -> None:
    """Reject a capped SOSL response whose total would be incomplete."""
    if len(records) >= 2000:
        raise SFAPIError(
            "Territory closed-won SOSL reached the 2000-record limit; refusing to report a potentially incomplete total"
        )
