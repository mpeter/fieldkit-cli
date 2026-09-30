"""fieldkit.sf.client — Thin synchronous httpx client for the Salesforce REST API.

Provides SOSL-based opportunity search and UI-API record fetch without
requiring browser automation. This module owns the synchronous Salesforce
data-access client used by commands and domain helpers.

Public API:
  SFDirectClient(session_id, base_url)          Stateful REST client.
  sf_errors.SFAuthError                                   Raised on HTTP 401 (re-auth needed).
  sf_errors.SFAPIError                                    Raised on all other HTTP / network errors.

Higher-level helpers:
  client.search_opportunities(keywords, account_name)          SOSL opportunity search → listview dicts.
  client.resolve_account_id_by_keywords(keywords, account_name) Fast Account.Id resolution via SOSL.
  client.fetch_account_by_id(account_id, fields)               Fetch Account sObject by ID.
  client.fetch_closeplan_deals(opp_id)                         Fetch all TSPC__Deal__c records with completeness.
  client.fetch_closeplan_questions(deal_id)                    Fetch all questions with completeness.
  client.fetch_closeplan_template(template_id)                 Fetch exact template version/maximum metadata.
  client.fetch_closeplan_template_question(template_question_id) Fetch exact native question metadata.
  client.fetch_closeplan_template_answers(template_question_id)  Fetch exact native answer choices.
"""

import logging
import re as _re
import types
import warnings
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any, cast

import httpx

from fieldkit.config.retry import (
    RETRY_MAX_ATTEMPTS,
    RETRY_TRANSIENT_STATUSES,
)
from fieldkit.sf import _responses, _transport
from fieldkit.sf import errors as sf_errors
from fieldkit.sf.types import (
    AccountResolution,
    DealSplitRecord,
    OpportunityRecord,
    OpportunitySearchResult,
    OpportunitySObject,
    SoslRecord,
    UIAPIRecordCollection,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_VERSION = "v59.0"
_SOSL_URL_PATH = f"/services/data/{API_VERSION}/search/"
_SOBJECT_PATH = f"/services/data/{API_VERSION}/sobjects/Opportunity/{{record_id}}"
_UI_API_CHILD_RELATIONSHIP_PAGE_SIZE = "100"

# SOSL field list for Opportunity searches.
# OpportunityNumber__c: the custom field API name for the SF Opportunity Number
# (the numeric join key for Varicent attainment reports, e.g. '71721820').
#
# The supported field is "OpportunityNumber__c", not "Opportunity_Number__c".
# An invalid field in a SOSL RETURNING clause rejects the whole search rather
# than yielding a null value. See fieldkit.sf.types for supported record shapes.
_OPPORTUNITY_FIELDS = (
    "Id, Name, StageName, CloseDate, Amount, Consulting_Total_USD__c, "
    "Training_Total_USD__c, Account.Id, OpportunityNumber__c"
)
_SERVICES_FILTER = "Consulting_Total_USD__c > 0 OR Training_Total_USD__c > 0"
# TAM is stored in Subscription and cannot safely widen this rollup filter.
# ``search_opportunity_candidates`` provides the opt-in quote-line-qualified path.

# Fields for Account sObject fetch via SOSL
_ACCOUNT_SOSL_FIELDS = "Id, Name, Industry, Owner.Name, Owner.Email, BillingCity, BillingState, Account_Segment__c"


def _http_date(value: str) -> str:
    """Convert a Salesforce UI API timestamp into an HTTP conditional-date value."""
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise sf_errors.SFAPIError("Salesforce conditional-write timestamp is invalid") from None
    if observed_at.tzinfo is None:
        raise sf_errors.SFAPIError("Salesforce conditional-write timestamp lacks a timezone")
    return format_datetime(observed_at.astimezone(UTC), usegmt=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _fmt_currency(value: Any) -> float | None:
    """Convert a raw Salesforce numeric field to a float, or None if absent."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# SOSL metacharacters that can break out of an expression.
# & is intentionally excluded — it appears in legitimate company names (AT&T, etc.)
# and is safe inside SOSL phrase quotes.
_SOSL_METACHAR_RE = _re.compile(r'[?|!(){}^~*:\\"]')


def _quote_keyword(keyword: str) -> str:
    """Sanitize and phrase-quote a keyword for safe SOSL expression embedding.

    Strips SOSL injection metacharacters before building the query.
    Preserves & (common in company names like AT&T, Procter & Gamble).
    Always phrase-quotes keywords that contain & or spaces.
    Raises ValueError if the keyword is empty or becomes empty after stripping.
    """
    sanitized = _SOSL_METACHAR_RE.sub("", keyword).strip()
    if sanitized != keyword:
        import click as _click

        _click.echo("Warning: SOSL keyword contained unsupported characters and was sanitized.", err=True)
    if not sanitized:
        raise ValueError("SOSL keyword is empty after sanitization. Remove or replace this keyword.")
    # Always phrase-quote if multi-word OR contains & (safe inside SOSL phrases)
    if " " in sanitized or "&" in sanitized:
        return f'"{sanitized}"'
    return sanitized


# ---------------------------------------------------------------------------
# SFDirectClient
# ---------------------------------------------------------------------------


class SFDirectClient:
    """Synchronous httpx client for the Salesforce REST API.

    Authenticates via the ``sid`` session cookie obtained from the browser.
    Use :func:`fieldkit.config.get_sf_session_id` to retrieve the sid and
    :func:`fieldkit.config.get_sf_rest_base_url` to derive the base_url from the
    org's Lightning URL.

    Args:
        session_id: The Salesforce session cookie value (``sid``).
        base_url:   The my.salesforce.com REST base URL, e.g.
                    ``https://yourorg.my.salesforce.com``.
    """

    def __init__(self, *, session_id: str, base_url: str) -> None:
        self._session_id = session_id
        self._base_url = base_url.rstrip("/")
        # G1a-2: httpx.Client is created lazily in __enter__, not here.
        # This prevents connection pool leaks when the caller forgets to use
        # the context manager — _require_open() raises RuntimeError instead of
        # silently leaking a connection.
        self._client: httpx.Client | None = None

    def __enter__(self) -> "SFDirectClient":
        # G1a-3: Construct the httpx.Client here so it is only created when
        # the caller explicitly enters the context manager.
        self._client = httpx.Client(timeout=httpx.Timeout(30.0))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __del__(self) -> None:
        """Emit ResourceWarning and close if garbage-collected without context manager exit."""
        if self._client is not None:
            warnings.warn(
                "SFDirectClient was garbage-collected without being closed. "
                "Use 'with SFDirectClient(...) as client:' to ensure cleanup.",
                ResourceWarning,
                # stacklevel=1: __del__ is invoked by the GC with no meaningful
                # call stack — stacklevel=1 points to this __del__ method itself,
                # which is the most informative location available.
                stacklevel=1,
            )
            self._client.close()
            self._client = None

    def _require_open(self) -> httpx.Client:
        """Return the active httpx.Client, raising RuntimeError if not in context manager.

        All request methods call this instead of accessing self._client directly.
        This ensures a clear error is raised when the caller forgets to use
        'with SFDirectClient(...) as client:' instead of silently leaking connections.

        Returns:
            The active httpx.Client instance.

        Raises:
            RuntimeError: If called outside a context manager (self._client is None).
        """
        if self._client is None:
            raise RuntimeError(
                "SFDirectClient must be used as a context manager: use 'with SFDirectClient(...) as client:'"
            )
        return self._client

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._session_id}"}

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Wrap ``self._client.request`` with retry logic for transient SF errors.

        Delegates to ``_transport._sf_request``, which selects the retry
        policy by HTTP method (see ``_transport._IDEMPOTENT_METHODS``).
        Raises immediately on 401 (auth failure, not retried).

        Args:
            method: HTTP method string, e.g. ``'GET'`` or ``'POST'``.
            url:    Fully-qualified URL to request.
            **kwargs: Passed through to ``httpx.Client.request``.

        Returns:
            The ``httpx.Response`` (any non-retryable status code).

        Raises:
            sf_errors.SFAuthError: immediately on HTTP 401.
            sf_errors.SFAPIError: after the selected retry policy stops on HTTP or transport errors.
        """
        try:
            resp = _transport._sf_request(self._require_open(), method, url, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise sf_errors.SFAuthError(
                    f"Salesforce authentication failed (HTTP 401). {sf_errors.reauth_hint_message()}"
                ) from None
            attempts = RETRY_MAX_ATTEMPTS if method.upper() in _transport._IDEMPOTENT_METHODS else 1
            raise sf_errors.SFAPIError(f"SF request failed after {attempts} attempts (last HTTP {status})") from None
        except httpx.RequestError:
            raise sf_errors.SFAPIError("SF request failed; check Salesforce availability and configuration.") from None
        if resp.status_code == 401:
            raise sf_errors.SFAuthError(
                f"Salesforce authentication failed (HTTP 401). {sf_errors.reauth_hint_message()}"
            )
        return resp

    # ------------------------------------------------------------------
    # Public API — low-level
    # ------------------------------------------------------------------

    def sosl_search(self, sosl_query: str) -> list[SoslRecord]:
        """Execute a SOSL query against the SF REST search endpoint.

        Args:
            sosl_query: A fully-formed SOSL string, e.g.
                ``FIND {Acme} IN ALL FIELDS RETURNING Opportunity(Id, Name)``.

        Returns:
            The ``searchRecords`` list from the JSON response, typed as
            :class:`~fieldkit.sf.types.SoslRecord`.  Only the fields present
            in the RETURNING clause of *sosl_query* will be populated.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        url = self._base_url + _SOSL_URL_PATH
        logger.debug("SOSL search started")
        resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"q": sosl_query})

        if resp.status_code != 200:
            logger.warning("SOSL error HTTP %s", resp.status_code)
            raise sf_errors.SFAPIError(
                f"SF SOSL search failed with HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )

        data = _responses._parse_json_response(resp, "SOSL search")
        raw_records = data.get("searchRecords")
        if not isinstance(raw_records, list) or any(not isinstance(record, dict) for record in raw_records):
            raise sf_errors.SFAPIError("SF SOSL search returned an invalid response shape")
        records = cast(list[SoslRecord], raw_records)
        logger.debug("SOSL returned %d record(s)", len(records))
        return records

    def fetch_record(self, record_id: str, fields: str | None = None) -> OpportunitySObject:
        """Fetch a single Salesforce Opportunity record via the sObject REST API.

        Args:
            record_id: The SF record ID (18-char or 15-char).
            fields:    Comma-separated API field names to return.  Defaults to
                       the standard opportunity field set defined by
                       ``_OPPORTUNITY_FIELDS``.  Pass a custom value to fetch
                       additional or different fields without touching private
                       internals.

        Returns:
            The parsed JSON response typed as
            :class:`~fieldkit.sf.types.OpportunitySObject`.  Only the fields
            requested via *fields* will be present; all others are absent
            (``total=False`` on the TypedDict keeps that legal).

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        # Fetch specific fields to avoid hitting field-level security limits
        fields = (fields or _OPPORTUNITY_FIELDS).replace(" ", "")
        path = _SOBJECT_PATH.format(record_id=record_id)
        url = self._base_url + path
        resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"fields": fields})

        if resp.status_code == 404:
            raise sf_errors.SFNotFoundError("Salesforce record not found.")
        if resp.status_code != 200:
            logger.warning("fetch_record error HTTP %s", resp.status_code)
            raise sf_errors.SFAPIError(
                f"SF record fetch failed with HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )

        return cast(OpportunitySObject, _responses._parse_json_response(resp, "fetch_record"))

    def fetch_sobject(
        self,
        sobject_type: str,
        record_id: str,
        fields: str,
    ) -> dict[str, Any]:
        """Fetch a single Salesforce sObject record of any type.

        Args:
            sobject_type: Salesforce object API name, e.g. ``"Account"``.
            record_id:    15- or 18-char SF record ID.
            fields:       Comma-separated API field names to return.

        Returns:
            The parsed JSON response dict.

        Raises:
            sf_errors.SFAuthError:    on HTTP 401.
            sf_errors.SFNotFoundError: on HTTP 404.
            sf_errors.SFAPIError:     on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/sobjects/{sobject_type}/{record_id}"
        resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"fields": fields})

        _responses._check_sobject_read_access(resp, sobject_type, record_id)
        if resp.status_code != 200:
            logger.warning("fetch_sobject error HTTP %s", resp.status_code)
            raise sf_errors.SFAPIError(
                f"SF record fetch failed with HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )
        return _responses._parse_json_response(resp, "fetch_sobject")

    def describe_sobject(self, sobject_type: str) -> dict[str, Any]:
        """Return describe metadata for a Salesforce sObject.

        Raises:
            sf_errors.SFAuthError: on HTTP 401 or an expired-session HTML login response.
            sf_errors.SFNotFoundError: on HTTP 404.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/sobjects/{sobject_type}/describe"
        resp = self._request_with_retry("GET", url, headers=self._auth_headers())

        if resp.status_code == 404:
            raise sf_errors.SFNotFoundError("Salesforce object not found.")
        if resp.status_code == 403:
            raise sf_errors.SFDataAccessError("Salesforce object is not readable; check permissions.")
        if resp.status_code != 200:
            logger.warning("describe_sobject error HTTP %s", resp.status_code)
            raise sf_errors.SFAPIError(
                f"SF object describe failed with HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )
        return _responses._parse_json_response(resp, "describe_sobject")

    def update_sobject_fields(
        self,
        sobject_type: str,
        record_id: str,
        fields: dict[str, Any],
    ) -> None:
        """PATCH a partial update to any Salesforce sObject record.

        Args:
            sobject_type: Salesforce object API name, e.g. ``"SBQQ__Quote__c"``.
            record_id:    15- or 18-char SF record ID.
            fields:       Dict of API field names to new values.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError:  on HTTP 400/403/4xx or connection errors.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/sobjects/{sobject_type}/{record_id}"
        # Absolute field assignments permit retrying transient responses.
        resp = self._request_with_retry(
            "PATCH",
            url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=fields,
        )
        # Note: 401 is already raised as sf_errors.SFAuthError by _request_with_retry — no re-check needed.
        if resp.status_code not in (200, 204):
            raise sf_errors.SFAPIError(
                f"SF object update failed HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )
        logger.debug("Salesforce object update succeeded (HTTP %s)", resp.status_code)

    def conditional_update_sobject_fields(
        self,
        sobject_type: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        if_unmodified_since: str,
    ) -> None:
        """PATCH one exact record once with Salesforce's stale-write precondition.

        Unlike :meth:`update_sobject_fields`, this method deliberately bypasses
        retry policy.  A transport failure or transient response cannot prove
        whether Salesforce applied the requested assignment, so callers must
        record an unknown outcome and reread the exact record before deciding
        what happened.

        Raises:
            sf_errors.SFAuthError: if Salesforce rejects the session (HTTP 401).
            sf_errors.SFConditionalWriteConflict: if Salesforce rejects the precondition
                as stale (HTTP 412).
            sf_errors.SFConditionalWriteOutcomeUnknown: if the request's effect cannot be
                established from its one response.
            sf_errors.SFAPIError: for a definite non-successful API response.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/sobjects/{sobject_type}/{record_id}"
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
            "If-Unmodified-Since": _http_date(if_unmodified_since),
        }
        try:
            response = self._require_open().request("PATCH", url, headers=headers, json=fields)
        except httpx.RequestError:
            raise sf_errors.SFConditionalWriteOutcomeUnknown(
                "Salesforce guarded write outcome is unknown; reread the exact record before another write."
            ) from None

        if response.status_code in (200, 204):
            return
        if response.status_code == 401:
            raise sf_errors.SFAuthError(
                f"Salesforce authentication failed (HTTP 401). {sf_errors.reauth_hint_message()}"
            )
        if response.status_code == 412:
            raise sf_errors.SFConditionalWriteConflict(
                "Salesforce rejected the guarded write because its precondition is stale."
            )
        if response.status_code in RETRY_TRANSIENT_STATUSES:
            raise sf_errors.SFConditionalWriteOutcomeUnknown(
                "Salesforce guarded write outcome is unknown; reread the exact record before another write."
            )
        raise sf_errors.SFAPIError(
            f"SF guarded object update failed HTTP {response.status_code}: {_responses._safe_error_detail(response)}"
        )

    def update_opportunity_fields(
        self,
        record_id: str,
        fields: dict[str, Any],
    ) -> None:
        """PATCH a partial update to a Salesforce Opportunity record.

        Args:
            record_id: 15- or 18-char SF Opportunity ID.
            fields:    Dict of API field names to new values.
                       Example: ``{"Next_Steps__c": "Schedule POC kickoff"}``

        Raises:
            sf_errors.SFAuthError: on HTTP 401 (session expired).
            sf_errors.SFAPIError:  on HTTP 400/403/4xx or connection errors.
        """
        path = _SOBJECT_PATH.format(record_id=record_id)
        url = self._base_url + path
        # Absolute field assignments permit retrying transient responses.
        resp = self._request_with_retry(
            "PATCH",
            url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=fields,
        )
        # Note: 401 is already raised as sf_errors.SFAuthError by _request_with_retry — no re-check needed.
        if resp.status_code not in (200, 204):
            raise sf_errors.SFAPIError(
                f"SF field update failed HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )
        logger.debug("Salesforce opportunity update succeeded (HTTP %s)", resp.status_code)

    # ------------------------------------------------------------------
    # Public API — higher-level
    # ------------------------------------------------------------------

    def _get_ui_api_records(
        self,
        url: str,
        params: dict[str, str],
        *,
        context: str,
        subject: str,
        error_label: str,
        not_found_issue: str | None = None,
    ) -> UIAPIRecordCollection:
        """GET UI API records with explicit collection-completeness evidence.

        Shared transport policy and collection validation serve the UI API
        call sites. A 404 is a
        complete empty relationship unless the caller requires count-bearing
        evidence. Missing or malformed records/count data returns an explicitly
        incomplete collection.

        ``context`` is the calling method name (log prefix + JSON-parse
        context), ``subject`` the record id for log messages, and
        ``error_label`` the phrase opening the sf_errors.SFAPIError raised on a non-200
        (e.g. ``"SF deal splits fetch"``), supplied as a fixed caller label.

        Raises:
            sf_errors.SFAuthError: on HTTP 401 (from ``_request_with_retry``).
            sf_errors.SFAPIError:  on connection failure or any non-200 other than 404.
        """
        resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params=params)

        if resp.status_code == 404:
            logger.debug("%s: record collection not found (HTTP 404)", context)
            if not_found_issue is not None:
                return UIAPIRecordCollection(
                    records=[],
                    reported_count=None,
                    complete=False,
                    issues=[not_found_issue],
                )
            return UIAPIRecordCollection(records=[], reported_count=0, complete=True, issues=[])
        if resp.status_code != 200:
            logger.warning("%s error HTTP %s", context, resp.status_code)
            raise sf_errors.SFAPIError(
                f"{error_label} failed with HTTP {resp.status_code}: {_responses._safe_error_detail(resp)}"
            )

        return _responses._ui_api_record_collection(
            _responses._parse_json_response(resp, context), context=context, subject=subject
        )

    def fetch_deal_splits(self, opp_id: str) -> list[DealSplitRecord]:
        """Fetch deal split records for a Salesforce Opportunity via the UI API.

        Uses the child-relationships endpoint which bypasses the SOQL TXN security
        policy that blocks direct SOQL queries.

        Args:
            opp_id: The 15- or 18-char Salesforce Opportunity ID.

        Returns:
            A list of dicts with keys ``offering_group`` (str) and
            ``services_pct`` (float).  Returns ``[]`` when no splits exist or
            the relationship is absent.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError:  on other HTTP errors or connection failures.
        """
        url = (
            f"{self._base_url}/services/data/{API_VERSION}/ui-api/records/{opp_id}/child-relationships/Deal_Splits1__r"
        )
        params = {"fields": "Deal_Splits__c.Offering_Group__c,Deal_Splits__c.Services_Percentage__c"}
        collection = self._get_ui_api_records(
            url,
            params,
            context="fetch_deal_splits",
            subject=opp_id,
            error_label="SF deal splits fetch",
        )
        return _responses._project_deal_splits(collection.records)

    def fetch_related_list_records(
        self,
        parent_id: str,
        related_list_id: str,
        fields: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch child records of a parent via the UI API related-list-records route.

        Some deployments block SOQL/SOSL for CPQ objects or do not expose
        ``OpportunityLineItems`` and ``ui-api/record-ui`` childRelationships.
        The ``related-list-records`` endpoint provides a compatible route for
        parent→child relationships such as:

          - Quote lines off a Quote → ``SBQQ__LineItems__r``
          - Quotes off an Opportunity → ``SBQQ__Quotes2__r``

        Args:
            parent_id:       15- or 18-char SF record ID of the parent (e.g. a Quote).
            related_list_id: The related list API name, e.g. ``SBQQ__LineItems__r``.
            fields:          Optional comma-separated qualified field names
                             (``ObjectApiName.FieldName``). When omitted, the
                             related list's configured columns are returned —
                             the most robust default on restricted deployments.

        Returns:
            The raw ``records`` list from the JSON response. Each element is a
            dict with an ``id`` and a ``fields`` dict keyed by field API name,
            where each value is ``{"value": ..., "displayValue": ...}``.
            Returns ``[]`` when the related list is empty or absent (HTTP 404).

        Raises:
            sf_errors.SFAuthError: on HTTP 401 (or an HTML login page).
            sf_errors.SFAPIError:  on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/ui-api/related-list-records/{parent_id}/{related_list_id}"
        params = {"fields": fields} if fields else {}
        collection = self._get_ui_api_records(
            url,
            params,
            context="fetch_related_list_records",
            subject=f"{parent_id}/{related_list_id}",
            error_label="SF related-list fetch",
        )
        logger.debug(
            "fetch_related_list_records: %d record(s)",
            len(collection.records),
        )
        return collection.records

    def search_opportunities(
        self,
        *,
        keywords: list[str],
        account_name: str,
    ) -> list[OpportunityRecord]:
        """Search for Salesforce opportunities using SOSL.

        Builds a SOSL query from the provided keywords, filters to records
        with Consulting or Training revenue, and maps results to the canonical
        listview dict shape used by the rest of the fieldkit SF pipeline.

        Args:
            keywords: Search terms (account name words, domain keywords, etc.).
                      Multi-word terms are automatically phrase-quoted in SOSL.
            account_name: The account name — used only for logging context.

        Returns:
            A list of opportunity dicts with the listview-compatible keys:
            ``opportunity_id``, ``name``, ``stage``, ``close_date``, ``arr``,
            ``owner``, ``next_steps``, ``acv``, ``consulting_acv``,
            ``training_acv``.  Returns ``[]`` when no opportunities match.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        return self._search_opportunities(keywords=keywords, account_name=account_name, services_only=True).records

    def search_opportunity_candidates(
        self,
        *,
        keywords: list[str],
        account_name: str,
    ) -> OpportunitySearchResult:
        """Return broad opportunity candidates and SOSL truncation metadata."""
        return self._search_opportunities(keywords=keywords, account_name=account_name, services_only=False)

    def _search_opportunities(
        self,
        *,
        keywords: list[str],
        account_name: str,
        services_only: bool,
    ) -> OpportunitySearchResult:
        if not keywords:
            logger.debug("search_opportunities: no keywords; returning empty")
            return OpportunitySearchResult(records=[], capped=False)

        quoted = " OR ".join(_quote_keyword(k) for k in keywords)
        # Use IN NAME FIELDS (not IN ALL FIELDS) to restrict matching to the
        # Opportunity Name field only.  IN ALL FIELDS also searches free-text
        # fields like Next_Steps__c, causing false-positive account matches when
        # next-steps text mentions another account's name.
        # Reaching the SOSL limit leaves result completeness unproven.
        where = f" WHERE {_SERVICES_FILTER}" if services_only else ""
        sosl = f"FIND {{{quoted}}} IN NAME FIELDS RETURNING Opportunity({_OPPORTUNITY_FIELDS}{where} LIMIT 2000)"

        logger.debug("searching opportunities with %d keyword(s)", len(keywords))
        records = self.sosl_search(sosl)
        if len(records) == 2000:
            logger.warning(
                "SOSL returned 2000 records (maximum); results may be truncated.",
            )

        results: list[OpportunityRecord] = []
        for rec in records:
            consulting_acv = _fmt_currency(rec.get("Consulting_Total_USD__c"))
            training_acv = _fmt_currency(rec.get("Training_Total_USD__c"))
            opp_num = rec.get("OpportunityNumber__c")
            results.append(
                {
                    "opportunity_id": rec.get("Id"),
                    "name": rec.get("Name"),
                    "stage": rec.get("StageName"),
                    "close_date": rec.get("CloseDate"),
                    "arr": None,
                    "owner": None,
                    "next_steps": None,
                    "acv": None,
                    "consulting_acv": consulting_acv,
                    "training_acv": training_acv,
                    "sf_opportunity_number": str(opp_num) if opp_num is not None else None,
                }
            )

        logger.debug("search_opportunities: %d opportunity/ies mapped", len(results))
        return OpportunitySearchResult(records=results, capped=len(records) == 2000)

    def resolve_account_id_by_keywords(
        self,
        *,
        keywords: list[str],
        account_name: str,
    ) -> str | None:
        """Resolve the Salesforce Account ID via SOSL opportunity search.

        Searches for opportunities matching the given keywords, then returns
        the Account.Id from the first result that carries one.  This is faster
        than scanning local pursuit files because it requires a single API call
        instead of file-by-file iteration.

        Args:
            keywords:     Account keywords used for SOSL discovery.
            account_name: Account configuration key retained for API compatibility.

        Returns:
            The 18-char Salesforce Account ID, or ``None`` if no match is found.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        if not keywords:
            logger.debug("resolve_account_id_by_keywords: no keywords")
            return None

        # Search with Account.Id included in RETURNING clause.
        # Use IN NAME FIELDS to restrict matching to Opportunity Name only —
        # same rationale as search_opportunities (implementation change).
        quoted = " OR ".join(_quote_keyword(k) for k in keywords)
        sosl = f"FIND {{{quoted}}} IN NAME FIELDS RETURNING Opportunity(Id, Account.Id WHERE {_SERVICES_FILTER} LIMIT 2000)"

        logger.debug("resolve_account_id_by_keywords: search started")
        records = self.sosl_search(sosl)

        if len(records) > 1:
            logger.debug(
                "resolve_account_id_by_keywords: %d candidate records",
                len(records),
            )

        for rec in records:
            account = rec.get("Account") or {}
            acct_id = account.get("Id")
            if acct_id:
                logger.debug("resolve_account_id_by_keywords: account resolved")
                return str(acct_id)

        logger.debug("resolve_account_id_by_keywords: no account found")
        return None

    def fetch_account_by_id(
        self,
        account_id: str,
        fields: str | None = None,
    ) -> AccountResolution | None:
        """Fetch a Salesforce Account record by its ID.

        Convenience wrapper around :meth:`fetch_sobject` for Account objects.

        Args:
            account_id: The 15- or 18-char Salesforce Account record ID.
            fields:     Comma-separated API field names. Defaults to
                        ``_ACCOUNT_SOSL_FIELDS``.

        Returns:
            The parsed Account record typed as
            :class:`~fieldkit.sf.types.AccountResolution`, or ``None`` if the
            record is not found.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError: on other HTTP errors or connection failures.
        """
        try:
            return cast(AccountResolution, self.fetch_sobject("Account", account_id, fields or _ACCOUNT_SOSL_FIELDS))
        except sf_errors.SFNotFoundError:
            logger.debug("fetch_account_by_id: account not found")
            return None

    def fetch_closeplan_deals(self, opp_id: str) -> UIAPIRecordCollection:
        """Fetch every ClosePlan deal linked to an Opportunity via UI API.

        Uses the UI API child-relationships endpoint (same pattern as
        :meth:`fetch_deal_splits`) to retrieve the ClosePlan scorecard rollup
        without SOQL.

        Args:
            opp_id: The 15- or 18-char Salesforce Opportunity ID.

        The maximum documented page size is requested. Because no sufficiently
        proven continuation-token contract is available for this relationship
        route, the returned collection is complete only when Salesforce's
        reported ``count`` equals the number of returned records.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError:  on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{API_VERSION}/ui-api/records/{opp_id}/child-relationships/TSPC__Deals__r"
        params = {
            "fields": (
                "TSPC__Deal__c.Id,"
                "TSPC__Deal__c.Name,"
                "TSPC__Deal__c.TSPC__ScorecardScoreRatio__c,"
                "TSPC__Deal__c.TSPC__ScorecardTotalScore__c,"
                "TSPC__Deal__c.TSPC__Template__c,"
                "TSPC__Deal__c.TSPC__TemplateDeployDate__c,"
                "TSPC__Deal__c.LastModifiedDate"
            ),
            "pageSize": _UI_API_CHILD_RELATIONSHIP_PAGE_SIZE,
        }
        return self._get_ui_api_records(
            url,
            params,
            context="fetch_closeplan_deals",
            subject=opp_id,
            error_label="SF ClosePlan fetch",
        )

    def fetch_closeplan_questions(self, deal_id: str) -> UIAPIRecordCollection:
        """Fetch TSPC__DealQuestion__c records for a ClosePlan deal via UI API.

        Retrieves per-element (MEDDPICC dimension) questions, scores, and
        answers linked to the given ``TSPC__Deal__c`` record.

        The supported relationship is
        ``TSPC__DealQuestions__r`` over ``TSPC__DealQuestion__c``. The client
        returns raw fields; the domain derives the MEDDPICC element from the
        question's ``Name`` prefix and display answer text from
        ``TSPC__TextAnswer__c`` or ``TSPC__RichTextAnswer__c``. Native answer
        fields remain separate from derived display text.
        See ``fieldkit.sf.meddpicc.extract_category`` / ``extract_answer`` for
        how the domain derives category and display answer text
        from these raw fields.

        Args:
            deal_id: The 15- or 18-char ``TSPC__Deal__c`` record ID.

        Returns:
            Raw records plus reported count and explicit completeness evidence.

        Raises:
            sf_errors.SFAuthError: on HTTP 401.
            sf_errors.SFAPIError:  on other HTTP errors or connection failures.
        """
        url = (
            f"{self._base_url}/services/data/{API_VERSION}"
            f"/ui-api/records/{deal_id}/child-relationships/TSPC__DealQuestions__r"
        )
        params = {
            "fields": (
                "TSPC__DealQuestion__c.Id,"
                "TSPC__DealQuestion__c.Name,"
                "TSPC__DealQuestion__c.TSPC__Score__c,"
                "TSPC__DealQuestion__c.TSPC__TextAnswer__c,"
                "TSPC__DealQuestion__c.TSPC__RichTextAnswer__c,"
                "TSPC__DealQuestion__c.TSPC__Answer__c,"
                "TSPC__DealQuestion__c.TSPC__MaxScore__c,"
                "TSPC__DealQuestion__c.TSPC__HasTextAnswer__c,"
                "TSPC__DealQuestion__c.TSPC__TemplateQuestion__c,"
                "TSPC__DealQuestion__c.LastModifiedDate"
            ),
            "pageSize": _UI_API_CHILD_RELATIONSHIP_PAGE_SIZE,
        }
        return self._get_ui_api_records(
            url,
            params,
            context="fetch_closeplan_questions",
            subject=deal_id,
            error_label="SF ClosePlan answers fetch",
        )

    def fetch_closeplan_template_question(self, template_question_id: str) -> dict[str, Any]:
        """Fetch one exact native ClosePlan template question without mutation."""
        return self.fetch_sobject(
            "TSPC__TemplateQuestion__c",
            template_question_id,
            (
                "Id,Name,TSPC__Template__c,TSPC__Category__c,TSPC__QuestionCategory__c,"
                "TSPC__Mode__c,TSPC__HasTextAnswer__c,TSPC__MaxScore__c,TSPC__Sync_ScoreField__c,"
                "TSPC__Sync_ScoreRatioField__c,TSPC__HasSharedScore__c"
            ),
        )

    def fetch_closeplan_template(self, template_id: str) -> dict[str, Any]:
        """Fetch one exact native ClosePlan template without mutation."""
        return self.fetch_sobject(
            "TSPC__Template__c",
            template_id,
            ("Id,TSPC__Version__c,TSPC__VersionName__c,TSPC__Type__c,TSPC__Status__c,TSPC__SC_TotalMaxScore__c"),
        )

    def fetch_closeplan_template_answers(self, template_question_id: str) -> UIAPIRecordCollection:
        """Fetch every exact answer choice defined by a ClosePlan template question."""
        url = (
            f"{self._base_url}/services/data/{API_VERSION}"
            f"/ui-api/records/{template_question_id}/child-relationships/TSPC__Answers__r"
        )
        params = {
            "fields": (
                "TSPC__TemplateQuestionAnswer__c.Id,"
                "TSPC__TemplateQuestionAnswer__c.Name,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__Text__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__Attitude__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__HasTextAnswer__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__MaxScore__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__SortOrder__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__Sync_TextAnswerField__c,"
                "TSPC__TemplateQuestionAnswer__c.TSPC__Sync_ValueFieldPickval__c"
            ),
            "pageSize": _UI_API_CHILD_RELATIONSHIP_PAGE_SIZE,
        }
        return self._get_ui_api_records(
            url,
            params,
            context="fetch_closeplan_template_answers",
            subject=template_question_id,
            error_label="SF ClosePlan template answers fetch",
            not_found_issue="template answer relationship was not found",
        )
