"""Salesforce opportunity-reference validation and resolution."""

import logging
import re

from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFDirectClient
from fieldkit.sf.errors import SFAPIError

logger = logging.getLogger(__name__)

OPPORTUNITY_ID_RE = re.compile(r"006(?:[A-Za-z0-9]{12}|[A-Za-z0-9]{15})\Z")
OPPORTUNITY_NUMBER_RE = re.compile(r"[0-9]{5,12}\Z")


def is_opportunity_id(value: str) -> bool:
    """Return whether *value* has the Opportunity prefix and supported ID shape."""
    return OPPORTUNITY_ID_RE.fullmatch(value) is not None


def validate_opportunity_binding(expected_id: str, payload_id: object, existing_id: object) -> None:
    """Refuse a sync that changes the requested record or an existing link.

    The case-sensitive first 15 characters identify a Salesforce record; its
    18-character form adds a case-insensitive checksum suffix.
    """
    if not is_opportunity_id(expected_id):
        raise FieldkitError("Salesforce opportunity identity is invalid")
    for candidate, required in ((payload_id, True), (existing_id, False)):
        if not required and candidate in (None, ""):
            continue
        if not isinstance(candidate, str) or not is_opportunity_id(candidate):
            raise FieldkitError("Salesforce opportunity identity is invalid")
        if candidate[:15] != expected_id[:15] or (len(candidate) == len(expected_id) and candidate != expected_id):
            raise FieldkitError("Salesforce opportunity identity does not match the requested record")


def is_opportunity_number(value: str) -> bool:
    """Return whether *value* is the supported numeric Opportunity Number form."""
    return OPPORTUNITY_NUMBER_RE.fullmatch(value) is not None


def resolve_opportunity_reference(client: SFDirectClient, reference: str) -> str | None:
    """Resolve an opportunity id or numeric Opportunity Number to a record id.

    Authentication failures propagate. Other Salesforce API failures degrade
    to an unresolved reference so the command can report a stable data error.
    """
    value = reference.strip()
    if is_opportunity_id(value):
        client.fetch_record(value, fields="Id")
        return value
    if not is_opportunity_number(value):
        return None

    sosl = f"FIND {{{value}}} IN ALL FIELDS RETURNING Opportunity(Id WHERE OpportunityNumber__c = '{value}')"
    try:
        records = client.sosl_search(sosl)
    except SFAPIError as exc:
        logger.warning("Opportunity number resolution failed: %s", exc)
        return None
    if not records:
        return None
    opp_id = records[0].get("Id")
    return opp_id if isinstance(opp_id, str) and is_opportunity_id(opp_id) else None
