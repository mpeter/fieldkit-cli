"""Current qualification states derived from native ClosePlan evidence."""

from typing import Literal

NativeQualificationStatus = Literal[
    "pending (live ClosePlan fetch required)",
    "unavailable (no Salesforce opportunity link)",
]


def native_qualification_status(opportunity_id: str | None) -> NativeQualificationStatus:
    """Describe whether a pursuit can obtain current ClosePlan qualification."""
    if opportunity_id:
        return "pending (live ClosePlan fetch required)"
    return "unavailable (no Salesforce opportunity link)"
