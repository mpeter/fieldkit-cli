"""fieldkit.tasks.classifier — Classify meeting action items for TASKS.md routing.

Three output classes:

  MY_TASK      — Owner is the AE (user_name / user_email). Goes into
                 TASKS.md Active section.

  WAITING_ON   — Owner is a customer stakeholder or external party, AND the item
                 contains a keyword signal that the outcome gates something the
                 AE tracks (deal mechanics, approvals, information needed, etc.).
                 Goes into TASKS.md Waiting On section.

  DROP         — Internal delivery team mechanics, personal items, Jira hygiene,
                 or generic team actions with no customer-facing gate.

Public API
----------
classify_action_item(item, user_name, user_email, stakeholder_names,
                     internal_team_names, *, pursuit_label) -> ClassifiedItem
classify_action_items(items, ...) -> list[ClassifiedItem]
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class ItemClass(StrEnum):
    MY_TASK = "my_task"
    WAITING_ON = "waiting_on"
    DROP = "drop"


@dataclass(frozen=True)
class ClassifiedItem:
    """A single classified action item."""

    text: str
    cls: ItemClass
    owner: str  # extracted owner name/label, or "Team" / "Unknown"
    rationale: str  # short explanation for why this classification was made
    pursuit_label: str  # e.g. "<account> / RHOAI" — for TASKS.md tag


# ---------------------------------------------------------------------------
# Keyword banks
# ---------------------------------------------------------------------------

# Deal paper & contracting
_KEYWORDS_DEAL = frozenset(
    {
        "purchase order",
        "po",
        "sow",
        "statement of work",
        "contract",
        "amendment",
        "redline",
        "red-line",
        "signature",
        "sign",
        "docusign",
        "countersign",
        "execute",
        "msa",
        "nda",
        "legal",
        "procurement",
        "legal review",
        "t&cs",
        "terms and conditions",
        "terms",
        "vehicle",
        "order form",
        "work order",
        "change order",
        "scope of work",
        "proposal",
        "quote",
        "pricing",
    }
)

# Approvals and decisions
_KEYWORDS_APPROVAL = frozenset(
    {
        "approve",
        "approval",
        "sign off",
        "sign-off",
        "green light",
        "go/no-go",
        "go no go",
        "decision",
        "confirm",
        "confirmation",
        "authorize",
        "authorization",
        "budget approval",
        "stakeholder approval",
        "leadership approval",
        "finance team",
        "finance",
        "budget",
        "fund",
        "funding",
        "sanctioned",
        "endorse",
        "endorsement",
        "committed",
    }
)

# Scheduling and meeting setup
_KEYWORDS_SCHEDULE = frozenset(
    {
        "schedule",
        "meeting invite",
        "calendar invite",
        "set up a call",
        "book",
        "reschedule",
        "invite",
        "availability",
        "sync",
        "set up",
        "arrange",
        "coordinate",
        "find time",
        "propose time",
        "block time",
        "agenda",
        "kickoff",
        "kick-off",
        "kick off",
    }
)

# Information the AE needs from the other party
_KEYWORDS_INFORMATION = frozenset(
    {
        "send",
        "share",
        "provide",
        "submit",
        "forward",
        "deliver",
        "get back",
        "loop back",
        "follow up",
        "follow-up",
        "circle back",
        "update",
        "respond",
        "reply",
        "let us know",
        "confirm back",
        "clarify",
        "email",
        "send over",
        "pass along",
        "give us",
        "get us",
        "bring",
        "report",
        "status",
        "feedback",
        "response",
        "answer",
        "input",
        "details",
        "information",
        "data",
        "numbers",
        "specs",
        "requirements",
        "documentation",
        "doc",
        "review",
        "assess",
        "evaluate",
    }
)

# Technical actions with business consequence
_KEYWORDS_TECHNICAL_GATE = frozenset(
    {
        "dependency",
        "blocker",
        "prerequisite",
        "requirement",
        "access",
        "credentials",
        "environment",
        "onboard",
        "onboarding",
        "install",
        "deploy",
        "go live",
        "go-live",
        "launch",
        "cut over",
        "cutover",
        "migrate",
        "migration",
        "production",
        "prod",
        "cluster",
        "tenant",
        "configure",
        "setup",
        "set up",
        "integrate",
        "integration",
        "api",
        "endpoint",
        "connect",
        "enable",
        "enablement",
        "stand up",
        "stand-up",
        "standup",
    }
)

# Milestones and dates
_KEYWORDS_MILESTONE = frozenset(
    {
        "mvp",
        "phase",
        "milestone",
        "deadline",
        "target date",
        "close date",
        "by friday",
        "by monday",
        "by tuesday",
        "by wednesday",
        "by thursday",
        "this week",
        "next week",
        "end of week",
        "eow",
        "eom",
        "eod",
        "end of month",
        "end of quarter",
        "end of year",
        "q1",
        "q2",
        "q3",
        "q4",
        "july 1",
        "june 30",
        "sprint",
        "release",
        "launch date",
        "go-live date",
        "due date",
        "due",
        "asap",
        "urgent",
        "critical",
        "blocking",
        "blocker",
        "today",
        "tomorrow",
        "before the call",
        "before the meeting",
    }
)

# Budget and financial
_KEYWORDS_FINANCIAL = frozenset(
    {
        "budget",
        "cost",
        "acv",
        "arr",
        "fiscal",
        "payment",
        "invoice",
        "billing",
        "revenue",
        "drawdown",
        "cu",
        "consulting unit",
        "rate",
        "rates",
        "hours",
        "capacity",
        "headcount",
    }
)

# Introductions and access grants
_KEYWORDS_CONNECT = frozenset(
    {
        "introduce",
        "introduction",
        "connect",
        "cc",
        "loop in",
        "include",
        "add",
        "get with",
        "reach out",
        "contact",
        "meet",
        "invite to",
        "onboard",
        "bring in",
        "involve",
        "engage",
    }
)

# Urgency markers — boost toward WAITING_ON regardless of category
_KEYWORDS_URGENCY = frozenset(
    {
        "asap",
        "urgent",
        "critical",
        "blocking",
        "blocker",
        "time-sensitive",
        "time sensitive",
        "immediately",
        "right away",
        "today",
        "tonight",
        "tomorrow",
        "this week",
        "before friday",
        "before monday",
        "before the call",
        "before the meeting",
        "by end of",
        "due date",
        "deadline",
        "overdue",
        "past due",
    }
)

# HIGH-CONFIDENCE gate keywords — deal mechanics, approvals, milestones, money.
# A named customer owner + ANY of these → WAITING_ON regardless of other signals.
_KEYWORDS_HIGH_CONFIDENCE: frozenset[str] = (
    _KEYWORDS_DEAL | _KEYWORDS_APPROVAL | _KEYWORDS_MILESTONE | _KEYWORDS_FINANCIAL
)

# BROAD coordination keywords — scheduling, info-passing, technical, intros.
# These alone are NOT sufficient for WAITING_ON; they require urgency or a date.
_KEYWORDS_BROAD: frozenset[str] = (
    _KEYWORDS_SCHEDULE | _KEYWORDS_INFORMATION | _KEYWORDS_TECHNICAL_GATE | _KEYWORDS_CONNECT
)

# All WAITING_ON signal keywords combined (kept for backward compatibility)
_KEYWORDS_WAITING_ON: frozenset[str] = _KEYWORDS_HIGH_CONFIDENCE | _KEYWORDS_BROAD | _KEYWORDS_URGENCY

# Internal delivery mechanics — strong DROP signals when owner is RH/Team
_KEYWORDS_INTERNAL_MECHANICS = frozenset(
    {
        "jira",
        "confluence",
        "ticket",
        "story",
        "epic",
        "sprint planning",
        "standup",
        "stand-up",
        "retro",
        "retrospective",
        "grooming",
        "backlog",
        "pr ",
        "pull request",
        "merge",
        "branch",
        "commit",
        "pipeline",
        "ci/cd",
        "build",
        "test",
        "unit test",
        "integration test",
        "qa",
        "code review",
        "review the code",
        "linting",
        "lint",
        "slack channel",
        "slack message",
        "teams channel",
        "laptop",
        "computer",
        "phone",
        "device",
        "hardware",
        "genius bar",
        "apple care",
        "it ticket",
        "it support",
        "password",
        "vpn",
        "badge",
        "access card",
        "expense",
        "travel",
        "hotel",
        "flight",
        "book travel",
        "personal",
        "dentist",
        "doctor",
        "appointment",
    }
)


# ---------------------------------------------------------------------------
# Owner extraction
# ---------------------------------------------------------------------------

# Patterns like "Alex Morgan: do X" or "Alex Morgan - do X" or "Alex to do X"
# Requires at least two capitalised words (first + last) OR a single word
# that is NOT a common label word like "Action", "Owner", "Team", "Note".
_LABEL_WORDS = frozenset(
    {
        "action",
        "owner",
        "note",
        "update",
        "team",
        "task",
        "item",
        "responsibility",
        "assigned",
        "follow",
        "status",
    }
)
_OWNER_PREFIX_RE = re.compile(
    r"^([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s*(?::|—|-|to\b)",
    re.IGNORECASE,
)
# Patterns like "Action: Alex Morgan" or "Owner: Brooke"
_OWNER_LABEL_RE = re.compile(
    r"(?:action item|action|owner|assigned to|assigned|responsibility|responsible)\s*[:\-]\s*([A-Z][a-z]+(?: [A-Z][a-z]+)*)",
    re.IGNORECASE,
)
# "Team:" prefix
_TEAM_PREFIX_RE = re.compile(r"^Team\s*[:\-]", re.IGNORECASE)


def _extract_owner(text: str) -> str:
    """Extract the owner name from an action item string.

    Returns the extracted name, "Team", or "Unknown".
    """
    # Team prefix
    if _TEAM_PREFIX_RE.match(text):
        return "Team"

    # "Name: task" or "Name — task" or "Name to task"
    m = _OWNER_PREFIX_RE.match(text.strip())
    if m:
        candidate = m.group(1).strip()
        # Reject single-word label words — they are not person names
        if candidate.lower() not in _LABEL_WORDS:
            return candidate

    # "Owner: Name" style
    m = _OWNER_LABEL_RE.search(text)
    if m:
        return m.group(1).strip()

    return "Unknown"


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


def _name_tokens(name: str) -> set[str]:
    """Return lowercase tokens from a name string."""
    return {w.lower() for w in re.split(r"[\s,.()\-]+", name) if len(w) >= 2}


def _name_matches(owner: str, candidate: str) -> bool:
    """Return True if *owner* matches *candidate* by token overlap.

    At least one non-trivial token (>= 3 chars) from candidate must appear
    in owner. Handles "Alex" matching "Alex Morgan" and vice versa.
    """
    if not owner or not candidate:
        return False
    owner_lower = owner.lower()
    candidate_tokens = _name_tokens(candidate)
    # Direct substring match first (fast path)
    if candidate.lower() in owner_lower or owner_lower in candidate.lower():
        return True
    # Token overlap
    meaningful = {t for t in candidate_tokens if len(t) >= 3}
    return bool(meaningful & _name_tokens(owner))


def _is_internal_team_member(owner: str, internal_team_names: Sequence[str]) -> bool:
    """Return True if owner matches any known internal team member name."""
    return any(_name_matches(owner, name) for name in internal_team_names)


def _is_customer_stakeholder(owner: str, stakeholder_names: Sequence[str]) -> bool:
    """Return True if owner matches any known customer stakeholder name."""
    return any(_name_matches(owner, name) for name in stakeholder_names)


def _is_user(owner: str, user_name: str, user_email: str) -> bool:
    """Return True if owner refers to the AE themselves."""
    if _name_matches(owner, user_name):
        return True
    local = user_email.split("@")[0].lower().replace(".", " ")
    return _name_matches(owner, local)


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------


def _has_high_confidence_signal(text: str) -> bool:
    """Return True if text contains a HIGH-CONFIDENCE deal-gate keyword.

    High-confidence = deal mechanics, approvals, milestones, or financial.
    These represent actions where the AE is genuinely waiting on the outcome.
    """
    t = text.lower()
    return any(kw in t for kw in _KEYWORDS_HIGH_CONFIDENCE)


def _has_broad_with_urgency(text: str) -> bool:
    """Return True if text has a broad coordination keyword AND urgency/date signal.

    Coordination items (schedule, info-pass, technical) are only worth tracking
    when they're time-pressured — otherwise they're delivery noise.
    """
    t = text.lower()
    has_broad = any(kw in t for kw in _KEYWORDS_BROAD)
    has_urgency = _has_urgency(t)
    # Also treat explicit date references as urgency (e.g. "by Wednesday", "before July 1")
    has_date = bool(
        re.search(
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|january|february|march|april|may|june|july|august|september|october|november|december"
            r"|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
            r"|\d{4}-\d{2}-\d{2}|by \w+day|end of \w+|q[1-4]\b)",
            t,
        )
    )
    return has_broad and (has_urgency or has_date)


def _has_internal_mechanics(text: str) -> bool:
    """Return True if text is predominantly internal delivery mechanics."""
    t = text.lower()
    return any(kw in t for kw in _KEYWORDS_INTERNAL_MECHANICS)


def _has_urgency(text: str) -> bool:
    """Return True if text contains an urgency marker."""
    t = text.lower()
    return any(kw in t for kw in _KEYWORDS_URGENCY)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _make_item(
    text: str,
    cls: ItemClass,
    owner: str,
    rationale: str,
    pursuit_label: str,
) -> ClassifiedItem:
    """Construct a ClassifiedItem (reduces repetition in classifier rules)."""
    return ClassifiedItem(text=text, cls=cls, owner=owner, rationale=rationale, pursuit_label=pursuit_label)


def _classify_ae_owner(
    item: str, owner: str, user_name: str, user_email: str, pursuit_label: str
) -> ClassifiedItem | None:
    """Rule 1: Owner is the AE → MY_TASK."""
    if _is_user(owner, user_name, user_email):
        return _make_item(item, ItemClass.MY_TASK, owner, "Owner matches AE name/email", pursuit_label)
    return None


def _classify_generic_internal(item: str, owner: str, pursuit_label: str) -> ClassifiedItem | None:
    """Rule 2: Generic team/unknown owner with internal mechanics → DROP."""
    if owner in ("Team", "Unknown") and _has_internal_mechanics(item):
        return _make_item(
            item, ItemClass.DROP, owner, "Generic team owner + internal mechanics keywords", pursuit_label
        )
    return None


def _classify_internal_team(
    item: str, owner: str, internal_team_names: Sequence[str], pursuit_label: str
) -> ClassifiedItem | None:
    """Rule 3: Internal delivery team member with only internal mechanics → DROP."""
    if _is_internal_team_member(owner, internal_team_names) and _has_internal_mechanics(item):
        return _make_item(
            item, ItemClass.DROP, owner, "Internal team member + internal mechanics — no customer gate", pursuit_label
        )
    return None


def _classify_customer_stakeholder(
    item: str, owner: str, stakeholder_names: Sequence[str], pursuit_label: str
) -> ClassifiedItem | None:
    """Rule 4: Customer stakeholder owner — requires deal gate or urgency signal."""
    if not _is_customer_stakeholder(owner, stakeholder_names):
        return None
    if _has_high_confidence_signal(item):
        return _make_item(
            item, ItemClass.WAITING_ON, owner, "Customer stakeholder + high-confidence deal gate", pursuit_label
        )
    if _has_broad_with_urgency(item):
        return _make_item(
            item,
            ItemClass.WAITING_ON,
            owner,
            "Customer stakeholder + coordination keyword with urgency/date",
            pursuit_label,
        )
    return _make_item(
        item, ItemClass.DROP, owner, "Customer stakeholder but no deal gate or urgency signal", pursuit_label
    )


def _classify_unknown_high_confidence(item: str, owner: str, pursuit_label: str) -> ClassifiedItem | None:
    """Rule 5: Unknown owner + HIGH-CONFIDENCE keyword → WAITING_ON."""
    if owner == "Unknown" and (_has_high_confidence_signal(item) or _has_urgency(item)):
        return _make_item(
            item, ItemClass.WAITING_ON, owner, "Unknown owner + high-confidence gate or urgency keyword", pursuit_label
        )
    return None


def _classify_internal_with_customer_gate(
    item: str,
    owner: str,
    stakeholder_names: Sequence[str],
    internal_team_names: Sequence[str],
    pursuit_label: str,
) -> ClassifiedItem | None:
    """Rule 6: Internal team member → WAITING_ON only for a named customer and high-confidence gate."""
    if not _is_internal_team_member(owner, internal_team_names):
        return None
    text_lower = item.lower()
    customer_mentioned = any(sn.split()[0].lower() in text_lower for sn in stakeholder_names if sn.strip())
    if customer_mentioned and _has_high_confidence_signal(item):
        return _make_item(
            item,
            ItemClass.WAITING_ON,
            owner,
            "Internal team member + named customer + high-confidence gate",
            pursuit_label,
        )
    return _make_item(item, ItemClass.DROP, owner, "Internal team member + no qualifying gate", pursuit_label)


def classify_action_item(
    item: str,
    *,
    user_name: str,
    user_email: str,
    stakeholder_names: Sequence[str],
    internal_team_names: Sequence[str],
    pursuit_label: str = "",
) -> ClassifiedItem:
    """Classify a single action item string.

    Args:
        item:               Raw action item text from meeting extraction.
        user_name:          Account engineer's full name from config.yaml (e.g. "Alex Morgan").
        user_email:         AE's email from config.yaml (e.g. "YOUR_EMAIL").
        stakeholder_names:  Known customer stakeholder names from the linked
                            pursuit file (used to identify WAITING_ON owners).
        internal_team_names: Known internal delivery team member names for the account.
        pursuit_label:      Label for TASKS.md tag e.g. "<account> / RHOAI".

    Returns:
        ClassifiedItem with cls, owner, and rationale populated.
    """
    owner = _extract_owner(item)

    result = (
        _classify_ae_owner(item, owner, user_name, user_email, pursuit_label)
        or _classify_generic_internal(item, owner, pursuit_label)
        or _classify_internal_team(item, owner, internal_team_names, pursuit_label)
        or _classify_customer_stakeholder(item, owner, stakeholder_names, pursuit_label)
        or _classify_unknown_high_confidence(item, owner, pursuit_label)
        or _classify_internal_with_customer_gate(item, owner, stakeholder_names, internal_team_names, pursuit_label)
    )
    if result is not None:
        return result

    # Rule 7: Team/Unknown with no signal → DROP
    return _make_item(item, ItemClass.DROP, owner, "No owner match, no gating keyword", pursuit_label)


def classify_action_items(
    items: Sequence[str],
    *,
    user_name: str,
    user_email: str,
    stakeholder_names: Sequence[str],
    internal_team_names: Sequence[str],
    pursuit_label: str = "",
) -> list[ClassifiedItem]:
    """Classify a list of action items. Returns all results including DROPs."""
    return [
        classify_action_item(
            item,
            user_name=user_name,
            user_email=user_email,
            stakeholder_names=stakeholder_names,
            internal_team_names=internal_team_names,
            pursuit_label=pursuit_label,
        )
        for item in items
    ]
