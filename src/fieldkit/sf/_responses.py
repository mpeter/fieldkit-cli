"""Private Salesforce response validation and UI API projection."""

import logging
from typing import Any, cast

import httpx

from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES
from fieldkit.sf import errors as sf_errors
from fieldkit.sf.types import DealSplitRecord, UIAPIRecordCollection

logger = logging.getLogger(__name__)

# The field names fetch_deal_splits() maps. Absence of BOTH is the signature
# of UI API response drift; presence with null values is a legitimately empty split.
_SPLIT_FIELD_NAMES = frozenset({"Offering_Group__c", "Services_Percentage__c"})


def _is_value_object(entry: Any) -> bool:
    """True if a UI API field entry still has the expected ``{"value": ...}`` shape.

    An object carrying only ``displayValue``, or a bare scalar, lacks this
    shape. Checking shape rather than nullity distinguishes drift from empty data.
    """
    return isinstance(entry, dict) and "value" in entry


def _check_sobject_read_access(response: httpx.Response, sobject_type: str, record_id: str) -> None:
    if response.status_code == 404:
        raise sf_errors.SFNotFoundError("Salesforce record not found.")
    if response.status_code == 403:
        raise sf_errors.SFDataAccessError("Salesforce record is not readable; check permissions.")


def _parse_json_response(resp: httpx.Response, context: str) -> dict[str, Any]:
    """Parse a JSON response, raising sf_errors.SFAuthError when SF returns HTML (expired session).

    SF can return `200 text/html` (login redirect) when the session expires.
    Calling resp.json() unconditionally crashes with JSONDecodeError — this function
    catches it and raises a clear sf_errors.SFAuthError with the auth sf hint.

    Callers must check `resp.status_code == 200` before calling this — SF error bodies
    are JSON arrays, not dicts, and are rejected below rather than silently miscast.
    """
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        raise sf_errors.SFAuthError(
            "Salesforce session expired — received HTML login page. Run 'fieldkit auth sf' to refresh."
        )
    try:
        data = resp.json()
    except ValueError:
        raise sf_errors.SFAPIError("SF response: failed to parse JSON response") from None
    if not isinstance(data, dict):
        raise sf_errors.SFAPIError("SF response: expected a JSON object response")
    return cast(dict[str, Any], data)


def _ui_api_record_collection(data: dict[str, Any], *, context: str, subject: str) -> UIAPIRecordCollection:
    """Validate UI API collection shape and retain explicit completeness evidence."""
    if "records" not in data:
        logger.debug("%s: response has no records collection", context)
        return UIAPIRecordCollection(
            records=[], reported_count=None, complete=False, issues=["records collection is missing"]
        )
    raw = data.get("records")
    if not isinstance(raw, list):
        logger.debug("%s: unexpected response shape", context)
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
    """Map status to fixed guidance without reading server-controlled payloads."""
    if resp.status_code == 401:
        return sf_errors.reauth_hint_message()
    if resp.status_code in (400, 403, 404, 405, 409, 412, 422):
        return "Request rejected; check the request data and permissions."
    if resp.status_code in RETRY_TRANSIENT_STATUSES:
        return "Service unavailable; retry later."
    return "Request failed; check Salesforce availability and configuration."


def _project_deal_splits(raw_records: list[dict[str, Any]]) -> list[DealSplitRecord]:
    """Project split values while reporting response-shape drift without private data."""
    # A reshaped UI API response (e.g. fields.X.displayValue replacing
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
            "fetch_deal_splits: %d of %d record(s) have no well-formed split fields "
            "— possible UI API response shape drift",
            drifted,
            len(raw_records),
        )
    else:
        logger.debug("fetch_deal_splits: %d split(s) found", len(results))
    return results
