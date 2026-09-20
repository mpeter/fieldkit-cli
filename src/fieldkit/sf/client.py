"""fieldkit.sf.client — Thin synchronous httpx client for the Salesforce REST API.

Provides SOSL-based opportunity search and UI-API record fetch without
requiring browser automation.  Playwright-based listview scraping was retired
in D009/D010; this module is the sole SF data access path.

Public API:
  SFDirectClient(session_id, base_url)          Stateful REST client.
  SFAuthError                                   Raised on HTTP 401 (re-auth needed).
  SFAPIError                                    Raised on all other HTTP / network errors.

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

from fieldkit.config import get_salesforce_org_url
from fieldkit.config.retry import (
    RETRY_MAX_ATTEMPTS,
    RETRY_TRANSIENT_STATUSES,
    connect_retry,
    transient_retry,
)
from fieldkit.errors import AuthError, FieldkitError
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

_API_VERSION = "v59.0"
_SOSL_URL_PATH = f"/services/data/{_API_VERSION}/search/"
_SOBJECT_PATH = f"/services/data/{_API_VERSION}/sobjects/Opportunity/{{record_id}}"
_UI_API_CHILD_RELATIONSHIP_PAGE_SIZE = "100"

# Retry policy lives in fieldkit.config.retry. _RETRY_BACKOFF (a leftover manual
# backoff table from before the implementation note tenacity migration) and its length assert
# are deleted here: tenacity computes the backoff, so the table was dead.
#
# PATCH is idempotent *for this client*: both PATCH call sites
# (update_sobject_fields, update_opportunity_fields) send an absolute field
# assignment via json=fields, so applying one twice leaves exactly the state
# applying it once does. historic regression routed them through retry deliberately and ships
# a test for it. POST is excluded — a resource-creating POST is not safe to repeat.
# There are no POST call sites today; this is a rail for the next one.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "PATCH"})

# SOSL field list for Opportunity searches.
# OpportunityNumber__c: the custom field API name for the SF Opportunity Number
# (the numeric join key for Varicent attainment reports, e.g. '71721820').
#
# VERIFIED (implementation change review HIGH-3, 2026-07-13): confirmed against the live org via
# GET /services/data/v59.0/sobjects/Opportunity/describe (your configured Salesforce host).
# The API name is "OpportunityNumber__c" (no underscores); "Opportunity_Number__c" does
# NOT exist (203 fields) and an invalid field in a SOSL RETURNING clause is rejected with
# HTTP 400 (INVALID_FIELD) — which breaks *every* Opportunity search (listview, pursuit
# matching), it does NOT silently return null.
_OPPORTUNITY_FIELDS = (
    "Id, Name, StageName, CloseDate, Amount, Consulting_Total_USD__c, "
    "Training_Total_USD__c, Account.Id, OpportunityNumber__c"
)
_SERVICES_FILTER = "Consulting_Total_USD__c > 0 OR Training_Total_USD__c > 0"
# TAM is stored in Subscription and cannot safely widen this rollup filter.
# ``search_opportunity_candidates`` provides the opt-in quote-line-qualified path.

# Fields for Account sObject fetch via SOSL
_ACCOUNT_SOSL_FIELDS = "Id, Name, Industry, Owner.Name, Owner.Email, BillingCity, BillingState, Account_Segment__c"

# historic regression: the field names fetch_deal_splits() maps. Absence of BOTH is the signature
# of UI API response drift; presence with null values is a legitimately empty split.
_SPLIT_FIELD_NAMES = frozenset({"Offering_Group__c", "Services_Percentage__c"})


def _http_date(value: str) -> str:
    """Convert a Salesforce UI API timestamp into an HTTP conditional-date value."""
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SFAPIError("Salesforce conditional-write timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise SFAPIError("Salesforce conditional-write timestamp lacks a timezone")
    return format_datetime(observed_at.astimezone(UTC), usegmt=True)


def _is_value_object(entry: Any) -> bool:
    """True if a UI API field entry still has the expected ``{"value": ...}`` shape.

    Drift observed in historic regression replaces that object with one carrying only
    ``displayValue``, or with a bare scalar. Either way the mapping silently yields
    nothing, so shape — not nullity — is what distinguishes drift from empty data.
    """
    return isinstance(entry, dict) and "value" in entry


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SFAuthError(AuthError):
    """Raised when the SF session ID is expired or invalid (HTTP 401).

    The caller should prompt the user to run ``fieldkit auth sf``
    to inject a fresh sid cookie from browser DevTools at yourorg.my.salesforce.com.
    """


class SFNotFoundError(FieldkitError):
    """Raised when the requested SF record does not exist (HTTP 404)."""


class SFDataAccessError(FieldkitError):
    """Raised when Salesforce denies access to a requested record (HTTP 403)."""


class SFAPIError(FieldkitError):
    """Raised on non-auth HTTP errors, connection failures, or unexpected responses."""


class SFConditionalWriteConflict(FieldkitError):
    """Raised when Salesforce rejects a guarded mutation as stale (HTTP 412)."""


class SFConditionalWriteOutcomeUnknown(FieldkitError):
    """Raised when a one-shot guarded mutation may have reached Salesforce."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _check_sobject_read_access(response: httpx.Response, sobject_type: str, record_id: str) -> None:
    if response.status_code == 404:
        raise SFNotFoundError(f"{sobject_type} {record_id} not found in Salesforce.")
    if response.status_code == 403:
        raise SFDataAccessError(f"{sobject_type} {record_id} is not readable in Salesforce.")


def reauth_hint_message() -> str:
    """Return a human-readable re-auth hint for Salesforce session errors.

    Derives the hint from the configured Salesforce org URL so the message
    names the actual org hostname rather than a generic placeholder.

    Uses ``get_salesforce_org_url()`` (NOT ``get_sf_rest_base_url()``) because
    the latter raises ``ConfigError`` on malformed URLs and must not be called
    from a help-text path.  This function MUST NOT raise under any config state.

    Returns:
        A string instructing the user to run ``fieldkit auth sf``
        with the org hostname if configured, or generic phrasing if not.
    """
    url = get_salesforce_org_url()
    if url:
        hostname = url.rstrip("/").removeprefix("https://").removeprefix("http://").split("/")[0]
        return f"run 'fieldkit auth sf' — copy the 'sid' cookie from browser DevTools at {hostname}"
    return "run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools"


# ---------------------------------------------------------------------------
# Retry helpers (implementation note: migrated from manual loop to tenacity)
# ---------------------------------------------------------------------------


def _is_sf_transient(exc: BaseException) -> bool:
    """True for retryable SF HTTP errors ({429, 500, 502, 503, 504})."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRY_TRANSIENT_STATUSES


def _raw_sf_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Make an HTTP request, raising httpx.HTTPStatusError on retryable status codes.

    Module-level function so tenacity can decorate it without binding to self.
    The caller (_request_with_retry) handles auth errors and non-retryable responses.

    Args:
        client: The httpx.Client to use.
        method: HTTP method string.
        url:    Fully-qualified URL.
        **kwargs: Passed through to client.request.

    Returns:
        The httpx.Response.

    Raises:
        httpx.HTTPStatusError: for status codes in RETRY_TRANSIENT_STATUSES (triggers retry).
        httpx.HTTPError:        for connection errors (propagates immediately, not retried).
    """
    resp = client.request(method, url, **kwargs)
    # Raise httpx.HTTPStatusError for retryable codes (tenacity retries on these)
    # and for 401 (not retryable — _is_sf_transient returns False, tenacity re-raises).
    # We raise manually rather than calling resp.raise_for_status() so the exception
    # is raised even when resp is a MagicMock in tests (MagicMock.raise_for_status()
    # returns a MagicMock rather than raising).
    if resp.status_code in RETRY_TRANSIENT_STATUSES or resp.status_code == 401:
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code}",
            request=httpx.Request(method, url),
            response=resp,
        )
    return resp


# One function serves reads and writes, so the policy cannot be chosen at
# decoration time. Decorate twice; dispatch on the method.
_sf_request_idempotent = transient_retry(_is_sf_transient, logger)(_raw_sf_request)
_sf_request_unsafe = connect_retry(logger)(_raw_sf_request)


def _sf_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Make an HTTP request under the retry policy appropriate to the method.

    Idempotent methods get the full transient policy (connect failures, timeouts,
    429/5xx). Anything else retries only connect-phase failures, where the request
    provably never reached the server and replaying it cannot duplicate an effect.
    """
    if method.upper() in _IDEMPOTENT_METHODS:
        return _sf_request_idempotent(client, method, url, **kwargs)
    return _sf_request_unsafe(client, method, url, **kwargs)


def _fmt_currency(value: Any) -> float | None:
    """Convert a raw Salesforce numeric field to a float, or None if absent."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# historic regression: SOSL metacharacters that can break out of a SOSL expression.
# & is intentionally excluded — it appears in legitimate company names (AT&T, etc.)
# and is safe inside SOSL phrase quotes.
_SOSL_METACHAR_RE = _re.compile(r'[?|!(){}^~*:\\"]')


def _quote_keyword(keyword: str) -> str:
    """Sanitize and phrase-quote a keyword for safe SOSL expression embedding.

    historic regression: Strips SOSL injection metacharacters before building the query.
    Preserves & (common in company names like AT&T, Procter & Gamble).
    Always phrase-quotes keywords that contain & or spaces.
    Raises ValueError if the keyword is empty or becomes empty after stripping.
    """
    sanitized = _SOSL_METACHAR_RE.sub("", keyword).strip()
    if sanitized != keyword:
        import click as _click

        _click.echo(
            f"Warning: SOSL keyword sanitized: {keyword!r} → {sanitized!r}",
            err=True,
        )
    if not sanitized:
        raise ValueError(f"SOSL keyword is empty after sanitization: {keyword!r}. Remove or replace this keyword.")
    # Always phrase-quote if multi-word OR contains & (safe inside SOSL phrases)
    if " " in sanitized or "&" in sanitized:
        return f'"{sanitized}"'
    return sanitized


# ---------------------------------------------------------------------------
# Safe error detail helper (historic regression)
# ---------------------------------------------------------------------------


def _parse_json_response(resp: httpx.Response, context: str) -> dict[str, Any]:
    """Parse a JSON response, raising SFAuthError when SF returns HTML (expired session).

    historic regression: SF returns `200 text/html` (login redirect) when the session expires.
    Calling resp.json() unconditionally crashes with JSONDecodeError — this function
    catches it and raises a clear SFAuthError with the auth sf hint.

    Callers must check `resp.status_code == 200` before calling this — SF error bodies
    are JSON arrays, not dicts, and are rejected below rather than silently miscast.
    """
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        raise SFAuthError("Salesforce session expired — received HTML login page. Run 'fieldkit auth sf' to refresh.")
    try:
        data = resp.json()
    except Exception as exc:
        raise SFAPIError(f"SF {context}: failed to parse JSON response (content-type {ct!r})") from exc
    if not isinstance(data, dict):
        raise SFAPIError(f"SF {context}: expected a JSON object response, got {type(data).__name__}")
    return cast(dict[str, Any], data)


def _ui_api_record_collection(data: dict[str, Any], *, context: str, subject: str) -> UIAPIRecordCollection:
    """Validate UI API collection shape and retain explicit completeness evidence."""
    if "records" not in data:
        logger.debug("%s: response has no records collection for %s", context, subject)
        return UIAPIRecordCollection(
            records=[], reported_count=None, complete=False, issues=["records collection is missing"]
        )
    raw = data.get("records")
    if not isinstance(raw, list):
        logger.debug("%s: unexpected response shape for %s", context, subject)
        return UIAPIRecordCollection(
            records=[], reported_count=None, complete=False, issues=["records collection has an invalid shape"]
        )
    records = [record for record in raw if isinstance(record, dict)]
    issues: list[str] = []
    malformed_count = len(raw) - len(records)
    if malformed_count:
        issues.append(f"records collection contains {malformed_count} non-object member(s)")
    reported_count = data.get("count")
    if isinstance(reported_count, bool) or not isinstance(reported_count, int) or reported_count < 0:
        issues.append("reported count is missing or invalid")
        return UIAPIRecordCollection(records=records, reported_count=None, complete=False, issues=issues)
    if reported_count != len(raw):
        issues.append(f"reported count {reported_count} does not match {len(raw)} returned record(s)")
    return UIAPIRecordCollection(
        records=records,
        reported_count=reported_count,
        complete=not issues,
        issues=issues,
    )


def _safe_error_detail(resp: httpx.Response) -> str:
    """Return a sanitized error summary from an SF API response.

    historic regression: SF error bodies can contain session tokens or PII. This function
    extracts only the top-level 'message' field from JSON errors, or returns
    a generic content-type descriptor for non-JSON responses (e.g. HTML login
    pages returned when the session expires).

    Never includes raw response body text in its output.
    """
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            return str(data[0].get("message", "(no message)"))
        if isinstance(data, dict):
            return str(data.get("message", "(no message)"))
    except Exception:  # noqa: BLE001
        pass
    ct = resp.headers.get("content-type", "unknown")
    return f"(non-JSON response, content-type={ct})"


# ---------------------------------------------------------------------------
# SFDirectClient
# ---------------------------------------------------------------------------


class SFDirectClient:
    """Synchronous httpx client for the Salesforce REST API.

    Authenticates via the ``sid`` session cookie obtained from the browser.
    Use :func:`lib.config.get_sf_session_id` to retrieve the sid and
    :func:`lib.config.get_sf_rest_base_url` to derive the base_url from the
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

        implementation note: Delegates to module-level ``_sf_request``, which selects the retry
        policy by HTTP method (see ``_IDEMPOTENT_METHODS``).
        Raises immediately on 401 (auth failure, not retried).

        Args:
            method: HTTP method string, e.g. ``'GET'`` or ``'POST'``.
            url:    Fully-qualified URL to request.
            **kwargs: Passed through to ``httpx.Client.request``.

        Returns:
            The ``httpx.Response`` (any non-retryable status code).

        Raises:
            SFAuthError: immediately on HTTP 401.
            SFAPIError:  after retries exhausted on transient codes.
        """
        try:
            resp = _sf_request(self._require_open(), method, url, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise SFAuthError(f"Salesforce authentication failed (HTTP 401). {reauth_hint_message()}") from exc
            raise SFAPIError(f"SF request failed after {RETRY_MAX_ATTEMPTS} attempts (last HTTP {status})") from exc
        if resp.status_code == 401:
            raise SFAuthError(f"Salesforce authentication failed (HTTP 401). {reauth_hint_message()}")
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
            SFAuthError: on HTTP 401.
            SFAPIError: on other HTTP errors or connection failures.
        """
        url = self._base_url + _SOSL_URL_PATH
        logger.debug("SOSL query: %r", sosl_query)
        try:
            resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"q": sosl_query})
        except httpx.ConnectError as exc:
            raise SFAPIError(f"SF connection failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise SFAPIError(f"SF request error: {exc}") from exc

        if resp.status_code != 200:
            logger.warning("SOSL error HTTP %s: %s", resp.status_code, _safe_error_detail(resp))
            raise SFAPIError(f"SF SOSL search failed with HTTP {resp.status_code}: {_safe_error_detail(resp)}")

        data = _parse_json_response(resp, "SOSL search")
        records: list[SoslRecord] = cast(list[SoslRecord], data.get("searchRecords", []))
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
            SFAuthError: on HTTP 401.
            SFAPIError: on other HTTP errors or connection failures.
        """
        # Fetch specific fields to avoid hitting field-level security limits
        fields = (fields or _OPPORTUNITY_FIELDS).replace(" ", "")
        path = _SOBJECT_PATH.format(record_id=record_id)
        url = self._base_url + path
        try:
            resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"fields": fields})
        except httpx.ConnectError as exc:
            raise SFAPIError(f"SF connection failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise SFAPIError(f"SF request error: {exc}") from exc

        if resp.status_code == 404:
            raise SFNotFoundError(f"Opportunity {record_id} not found in Salesforce.")
        if resp.status_code != 200:
            logger.warning("fetch_record error HTTP %s: %s", resp.status_code, _safe_error_detail(resp))
            raise SFAPIError(f"SF record fetch failed with HTTP {resp.status_code}: {_safe_error_detail(resp)}")

        return cast(OpportunitySObject, _parse_json_response(resp, "fetch_record"))

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
            SFAuthError:    on HTTP 401.
            SFNotFoundError: on HTTP 404.
            SFAPIError:     on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{_API_VERSION}/sobjects/{sobject_type}/{record_id}"
        try:
            resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params={"fields": fields})
        except httpx.ConnectError as exc:
            raise SFAPIError(f"SF connection failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise SFAPIError(f"SF request error: {exc}") from exc

        _check_sobject_read_access(resp, sobject_type, record_id)
        if resp.status_code != 200:
            logger.warning("fetch_sobject error HTTP %s: %s", resp.status_code, _safe_error_detail(resp))
            raise SFAPIError(f"SF {sobject_type} fetch failed with HTTP {resp.status_code}: {_safe_error_detail(resp)}")
        return _parse_json_response(resp, "fetch_sobject")

    def describe_sobject(self, sobject_type: str) -> dict[str, Any]:
        """Return describe metadata for a Salesforce sObject.

        Raises:
            SFAuthError: on HTTP 401 or an expired-session HTML login response.
            SFNotFoundError: on HTTP 404.
            SFAPIError: on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{_API_VERSION}/sobjects/{sobject_type}/describe"
        try:
            resp = self._request_with_retry("GET", url, headers=self._auth_headers())
        except httpx.ConnectError as exc:
            raise SFAPIError(f"SF connection failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise SFAPIError(f"SF request error: {exc}") from exc

        if resp.status_code == 404:
            raise SFNotFoundError(f"Salesforce object {sobject_type} not found.")
        if resp.status_code == 403:
            raise SFDataAccessError(f"Salesforce object {sobject_type} is not readable.")
        if resp.status_code != 200:
            logger.warning("describe_sobject error HTTP %s: %s", resp.status_code, _safe_error_detail(resp))
            raise SFAPIError(
                f"SF {sobject_type} describe failed with HTTP {resp.status_code}: {_safe_error_detail(resp)}"
            )
        return _parse_json_response(resp, "describe_sobject")

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
            SFAuthError: on HTTP 401.
            SFAPIError:  on HTTP 400/403/4xx or connection errors.
        """
        url = f"{self._base_url}/services/data/{_API_VERSION}/sobjects/{sobject_type}/{record_id}"
        # historic regression: route through _request_with_retry so transient 5xx errors are retried
        resp = self._request_with_retry(
            "PATCH",
            url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=fields,
        )
        # Note: 401 is already raised as SFAuthError by _request_with_retry — no re-check needed.
        if resp.status_code not in (200, 204):
            raise SFAPIError(f"SF {sobject_type} update failed HTTP {resp.status_code}: {_safe_error_detail(resp)}")
        logger.debug("update_sobject_fields: %s %s updated OK (HTTP %s)", sobject_type, record_id, resp.status_code)

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
            SFAuthError: if Salesforce rejects the session (HTTP 401).
            SFConditionalWriteConflict: if Salesforce rejects the precondition
                as stale (HTTP 412).
            SFConditionalWriteOutcomeUnknown: if the request's effect cannot be
                established from its one response.
            SFAPIError: for a definite non-successful API response.
        """
        url = f"{self._base_url}/services/data/{_API_VERSION}/sobjects/{sobject_type}/{record_id}"
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
            "If-Unmodified-Since": _http_date(if_unmodified_since),
        }
        try:
            response = self._require_open().request("PATCH", url, headers=headers, json=fields)
        except httpx.RequestError as exc:
            raise SFConditionalWriteOutcomeUnknown(
                "Salesforce guarded write outcome is unknown; reread the exact record before another write."
            ) from exc

        if response.status_code in (200, 204):
            return
        if response.status_code == 401:
            raise SFAuthError(f"Salesforce authentication failed (HTTP 401). {reauth_hint_message()}")
        if response.status_code == 412:
            raise SFConditionalWriteConflict("Salesforce rejected the guarded write because its precondition is stale.")
        if response.status_code in RETRY_TRANSIENT_STATUSES:
            raise SFConditionalWriteOutcomeUnknown(
                "Salesforce guarded write outcome is unknown; reread the exact record before another write."
            )
        raise SFAPIError(
            f"SF guarded {sobject_type} update failed HTTP {response.status_code}: {_safe_error_detail(response)}"
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
            SFAuthError: on HTTP 401 (session expired).
            SFAPIError:  on HTTP 400/403/4xx or connection errors.
        """
        path = _SOBJECT_PATH.format(record_id=record_id)
        url = self._base_url + path
        # historic regression: route through _request_with_retry so transient 5xx errors are retried
        resp = self._request_with_retry(
            "PATCH",
            url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=fields,
        )
        # Note: 401 is already raised as SFAuthError by _request_with_retry — no re-check needed.
        if resp.status_code not in (200, 204):
            raise SFAPIError(f"SF field update failed HTTP {resp.status_code}: {_safe_error_detail(resp)}")
        logger.debug("update_opportunity_fields: record %s updated OK (HTTP %s)", record_id, resp.status_code)

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

        The connect-error / request-error / 404 / non-200 / non-list-shape
        preamble, once, for the UI API call sites that shared it. A 404 is a
        complete empty relationship unless the caller requires count-bearing
        evidence. Missing or malformed records/count data returns an explicitly
        incomplete collection.

        ``context`` is the calling method name (log prefix + JSON-parse
        context), ``subject`` the record id for log messages, and
        ``error_label`` the phrase opening the SFAPIError raised on a non-200
        (e.g. ``"SF deal splits fetch"``), which keeps each caller's message
        byte-identical.

        Raises:
            SFAuthError: on HTTP 401 (from ``_request_with_retry``).
            SFAPIError:  on connection failure or any non-200 other than 404.
        """
        try:
            resp = self._request_with_retry("GET", url, headers=self._auth_headers(), params=params)
        except httpx.ConnectError as exc:
            raise SFAPIError(f"SF connection failed: {exc}") from exc
        except httpx.RequestError as exc:
            raise SFAPIError(f"SF request error: {exc}") from exc

        if resp.status_code == 404:
            logger.debug("%s: not found for %s (404)", context, subject)
            if not_found_issue is not None:
                return UIAPIRecordCollection(
                    records=[],
                    reported_count=None,
                    complete=False,
                    issues=[not_found_issue],
                )
            return UIAPIRecordCollection(records=[], reported_count=0, complete=True, issues=[])
        if resp.status_code != 200:
            logger.warning("%s error HTTP %s: %s", context, resp.status_code, _safe_error_detail(resp))
            raise SFAPIError(f"{error_label} failed with HTTP {resp.status_code}: {_safe_error_detail(resp)}")

        return _ui_api_record_collection(_parse_json_response(resp, context), context=context, subject=subject)

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
            SFAuthError: on HTTP 401.
            SFAPIError:  on other HTTP errors or connection failures.
        """
        url = (
            f"{self._base_url}/services/data/{_API_VERSION}/ui-api/records/{opp_id}/child-relationships/Deal_Splits1__r"
        )
        params = {"fields": "Deal_Splits__c.Offering_Group__c,Deal_Splits__c.Services_Percentage__c"}
        collection = self._get_ui_api_records(
            url,
            params,
            context="fetch_deal_splits",
            subject=opp_id,
            error_label="SF deal splits fetch",
        )
        raw_records = collection.records

        # historic regression: a reshaped UI API response (e.g. fields.X.displayValue replacing
        # fields.X.value) makes every record fail the "both values None" check, so the
        # method returns [] with no signal. The discriminator for drift is whether the
        # expected KEYS are present — not whether their values are null. A split row
        # that legitimately has no values yet is normal, and warning about it would
        # train operators to ignore the warning before it ever fires for real drift.
        results: list[DealSplitRecord] = []
        drifted = 0
        for rec in raw_records:
            fields = rec.get("fields", {})
            if not isinstance(fields, dict) or _SPLIT_FIELD_NAMES.isdisjoint(fields):
                drifted += 1
                continue
            if any(not _is_value_object(fields[n]) for n in _SPLIT_FIELD_NAMES if n in fields):
                drifted += 1
                continue
            offering_group_raw = fields.get("Offering_Group__c", {}).get("value")
            services_pct_raw = fields.get("Services_Percentage__c", {}).get("value")
            if offering_group_raw is None and services_pct_raw is None:
                # Present, well-formed, and unvalued — a legitimately empty split row.
                continue
            results.append(
                {
                    "offering_group": str(offering_group_raw) if offering_group_raw is not None else "",
                    "services_pct": float(services_pct_raw) if services_pct_raw is not None else 0.0,
                }
            )

        if drifted:
            logger.warning(
                "fetch_deal_splits: %d of %d record(s) for %s have no well-formed %r field "
                "— possible UI API response shape drift",
                drifted,
                len(raw_records),
                opp_id,
                sorted(_SPLIT_FIELD_NAMES),
            )
        else:
            logger.debug("fetch_deal_splits: %d split(s) found for %s", len(results), opp_id)
        return results

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
            SFAuthError: on HTTP 401 (or an HTML login page).
            SFAPIError:  on other HTTP errors or connection failures.
        """
        url = f"{self._base_url}/services/data/{_API_VERSION}/ui-api/related-list-records/{parent_id}/{related_list_id}"
        params = {"fields": fields} if fields else {}
        collection = self._get_ui_api_records(
            url,
            params,
            context="fetch_related_list_records",
            subject=f"{parent_id}/{related_list_id}",
            error_label="SF related-list fetch",
        )
        logger.debug(
            "fetch_related_list_records: %d record(s) for %s/%s",
            len(collection.records),
            parent_id,
            related_list_id,
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
            SFAuthError: on HTTP 401.
            SFAPIError: on other HTTP errors or connection failures.
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
            logger.debug("search_opportunities: no keywords for %r, returning empty", account_name)
            return OpportunitySearchResult(records=[], capped=False)

        quoted = " OR ".join(_quote_keyword(k) for k in keywords)
        # Use IN NAME FIELDS (not IN ALL FIELDS) to restrict matching to the
        # Opportunity Name field only.  IN ALL FIELDS also searches free-text
        # fields like Next_Steps__c, causing false-positive account matches when
        # next-steps text mentions another account's name.  (implementation change)
        # historic regression: add LIMIT 2000 (SF max) to SOSL RETURNING clause; warn if at cap
        where = f" WHERE {_SERVICES_FILTER}" if services_only else ""
        sosl = f"FIND {{{quoted}}} IN NAME FIELDS RETURNING Opportunity({_OPPORTUNITY_FIELDS}{where} LIMIT 2000)"

        logger.debug("searching opportunities for account %r with %d keyword(s)", account_name, len(keywords))
        records = self.sosl_search(sosl)
        if len(records) == 2000:
            logger.warning(
                "SOSL returned 2000 records (maximum) for account %r — results may be truncated.",
                account_name,
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

        logger.debug("search_opportunities: %d opportunity/ies mapped for %r", len(results), account_name)
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
            account_name: Used for logging context only.

        Returns:
            The 18-char Salesforce Account ID, or ``None`` if no match is found.

        Raises:
            SFAuthError: on HTTP 401.
            SFAPIError: on other HTTP errors or connection failures.
        """
        if not keywords:
            logger.debug("resolve_account_id_by_keywords: no keywords for %r", account_name)
            return None

        # Search with Account.Id included in RETURNING clause.
        # Use IN NAME FIELDS to restrict matching to Opportunity Name only —
        # same rationale as search_opportunities (implementation change).
        quoted = " OR ".join(_quote_keyword(k) for k in keywords)
        sosl = f"FIND {{{quoted}}} IN NAME FIELDS RETURNING Opportunity(Id, Account.Id WHERE {_SERVICES_FILTER} LIMIT 2000)"

        logger.debug("resolve_account_id_by_keywords: SOSL for account %r", account_name)
        try:
            records = self.sosl_search(sosl)
        except SFAPIError:
            logger.debug("resolve_account_id_by_keywords: SOSL failed for %r", account_name)
            return None

        if len(records) > 1:
            logger.debug(
                "SOSL multi-candidate accounts for %r: %s",
                account_name,
                [(r.get("Account") or {}).get("Id") for r in records],
            )

        for rec in records:
            account = rec.get("Account") or {}
            acct_id = account.get("Id")
            if acct_id:
                logger.debug("resolve_account_id_by_keywords: resolved %r → Account %s", account_name, acct_id)
                return str(acct_id)

        logger.debug("resolve_account_id_by_keywords: no Account.Id found for %r", account_name)
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
            SFAuthError: on HTTP 401.
            SFAPIError: on other HTTP errors or connection failures.
        """
        try:
            return cast(AccountResolution, self.fetch_sobject("Account", account_id, fields or _ACCOUNT_SOSL_FIELDS))
        except SFNotFoundError:
            logger.debug("fetch_account_by_id: Account %s not found", account_id)
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
            SFAuthError: on HTTP 401.
            SFAPIError:  on other HTTP errors or connection failures.
        """
        url = (
            f"{self._base_url}/services/data/{_API_VERSION}/ui-api/records/{opp_id}/child-relationships/TSPC__Deals__r"
        )
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

        historic regression: this previously queried ``TSPC__DealQuestionAnswers__r`` over
        a ``TSPC__DealQuestionAnswer__c`` object with a ``TSPC__CategoryName__c``
        field -- these fields are not part of the supported schema observed via
        ``GET /ui-api/object-info/TSPC__Deal__c``, which previously crashed
        this method on every call. The real relationship is
        ``TSPC__DealQuestions__r`` over ``TSPC__DealQuestion__c``. There is no
        separate category field: the MEDDPICC element name is embedded as a
        prefix in the question's own ``Name`` (e.g. "ECONOMIC BUYER -
        Individual within..."), and the answer lives in ``TSPC__TextAnswer__c``
        or ``TSPC__RichTextAnswer__c`` rather than a single ``TSPC__Answer__c``.
        See ``fieldkit.sf.meddpicc.extract_category`` / ``extract_answer`` for
        how the domain derives category and display answer text
        from these raw fields.

        Args:
            deal_id: The 15- or 18-char ``TSPC__Deal__c`` record ID.

        Returns:
            Raw records plus reported count and explicit completeness evidence.

        Raises:
            SFAuthError: on HTTP 401.
            SFAPIError:  on other HTTP errors or connection failures.
        """
        url = (
            f"{self._base_url}/services/data/{_API_VERSION}"
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
            f"{self._base_url}/services/data/{_API_VERSION}"
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
