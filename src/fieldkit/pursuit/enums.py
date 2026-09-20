"""Canonical enums for pursuit domain constants.

Stage and MEDDPICCElement use StrEnum so values compare equal to their
string equivalents (e.g. Stage.CLOSED_WON == "closed-won" is True).
This makes them drop-in replacements for raw string literals in dicts,
Literal type annotations, and YAML-loaded data.
"""

from enum import StrEnum


class Stage(StrEnum):
    """Pipeline stage names as used in pursuit frontmatter YAML."""

    PRE_PIPELINE = "pre-pipeline"
    PROSPECT = "prospect"
    QUALIFY = "qualify"
    DISCOVER = "discover"
    VALIDATE = "validate"
    PROPOSE = "propose"
    NEGOTIATE = "negotiate"
    CLOSED_WON = "closed-won"
    CLOSED_LOST = "closed-lost"


class MEDDPICCElement(StrEnum):
    """MEDDPICC element keys as used in pursuit frontmatter YAML (hyphenated form)."""

    METRICS = "metrics"
    ECONOMIC_BUYER = "economic-buyer"
    DECISION_CRITERIA = "decision-criteria"
    DECISION_PROCESS = "decision-process"
    IDENTIFY_PAIN = "identify-pain"
    CHAMPION = "champion"
    COMPETITION = "competition"
    PAPER_PROCESS = "paper-process"
