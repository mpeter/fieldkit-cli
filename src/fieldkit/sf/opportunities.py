"""Salesforce opportunity-reference validation and resolution."""

import logging
import re

from fieldkit.sf.client import SFAPIError, SFDirectClient

logger = logging.getLogger(__name__)

OPPORTUNITY_ID_RE = re.compile(r"[A-Za-z0-9]{15}\Z|[A-Za-z0-9]{18}\Z")
OPPORTUNITY_NUMBER_RE = re.compile(r"[0-9]{5,12}\Z")


def is_opportunity_id(value: str) -> bool:
    """Return whether *value* is a 15- or 18-character Salesforce id."""
    return OPPORTUNITY_ID_RE.fullmatch(value) is not None


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
    return str(opp_id) if opp_id else None
