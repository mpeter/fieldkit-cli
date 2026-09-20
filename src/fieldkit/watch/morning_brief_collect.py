"""Data collection helpers for the morning brief watcher — domain module.

Moved from ``commands/watch/morning_brief_collect.py`` (watch-domain-migration,
implementation change slice 2.9). No Click imports — pure data collection logic.

Contains calendar fetch, pursuit pulse, cross-account signal detection,
and alert extraction functions.  Extracted from morning_brief.py to keep
each concern in its own module.
"""

import json
import logging
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fieldkit.config import get_account_names, get_accounts_config, get_fieldkit_home, get_user_email_from_env
from fieldkit.pursuit.io import load_pursuit, parse_frontmatter
from fieldkit.pursuit.stages import CLOSED_STAGES as _CLOSED_STAGE_NAMES
from fieldkit.watch.morning_brief_mcp import MCPSession

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _parse_internal_domains(config: dict[str, Any]) -> set[str]:
    """Return the set of internal (non-customer) email domains.

    Priority: accounts.yaml internal_domains key (via config dict) →
    lib.config.get_internal_domains() → empty set (caller handles absence).
    """
    from fieldkit.config import get_internal_domains

    raw = config.get("internal_domains")
    if isinstance(raw, list) and raw:
        return {d.lower() for d in raw if isinstance(d, str)}
    cfg_domains = get_internal_domains()
    return {d.lower() for d in cfg_domains} if cfg_domains else set()


# ---------------------------------------------------------------------------
# Alert extraction helpers
# ---------------------------------------------------------------------------


def extract_today_alerts(alert_file: Path, today: date, lookback_days: int = 1) -> list[str]:
    """Extract recent alert blocks from a watcher alert file.

    Each alert block starts with a ``## YYYY-MM-DD`` heading and continues
    until the next ``##`` heading or end of file.  Blocks whose date falls
    within the half-open window ``[today - lookback_days, today]`` (inclusive
    on both ends) are returned as a list of markdown block strings.

    The default ``lookback_days=1`` includes both today's and yesterday's
    alerts so that alerts written late in the previous day are not silently
    dropped when the brief runs early the next morning (historic regression).

    HTML run-summary comments (``<!-- ... -->``) are stripped from each block
    body before it is returned (historic regression).
    """
    if not alert_file.exists():
        raise FileNotFoundError(f"{alert_file} does not exist")

    text = alert_file.read_text(encoding="utf-8")
    cutoff = today - timedelta(days=lookback_days)

    splits = _ALERT_HEADING_RE.split(text)
    alerts: list[str] = []
    seen: set[str] = set()
    i = 1
    while i < len(splits) - 1:
        heading = splits[i].strip()
        body = splits[i + 1]
        # Extract the ISO date from the heading (characters 3-12 after "## ")
        try:
            alert_date = date.fromisoformat(heading[3:13])
        except ValueError:
            i += 2
            continue
        if cutoff <= alert_date <= today:
            # Strip HTML run-summary comments before presenting the block body.
            clean_body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
            block = f"{heading}\n\n{clean_body}" if clean_body else heading
            if heading not in seen:
                seen.add(heading)
                alerts.append(block)
        i += 2

    return alerts


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

_CALENDAR_NO_EVENTS_RE = re.compile(r"^\s*No events found in calendar\b", re.IGNORECASE)


def _email_domain(email: str) -> str:
    """Return the domain part of *email* (lowercased), or '' if unparseable."""
    parts = email.strip().lower().rsplit("@", 1)
    return parts[1] if len(parts) == 2 else ""


def resolve_user_email(config: dict[str, Any]) -> str:
    """Resolve the AE's own Google email for MCP calls.

    Tries (in order):
    1. FIELDKIT_USER_EMAIL environment variable
    2. USER environment variable + ``email_domain`` from config.yaml
    3. USER environment variable + domain derived from the ``email`` config key
    4. Falls back to the empty string (caller must handle).

    Configure ``email_domain`` in config.yaml (e.g. ``email_domain: example.com``)
    to avoid relying on a hardcoded org domain.

    Args:
        config: Unused; retained for API compatibility with existing callers.
    """
    return get_user_email_from_env() or ""


def _calendar_items(value: Any) -> list[Any]:
    """Extract a list of events from an MCP JSON value, or return no events."""
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, dict):
        return []
    items = value.get("items") or value.get("events") or []
    return list(items) if isinstance(items, list) else []


def _parse_calendar_text_or_raise(result: str) -> list[dict[str, Any]]:
    """Return a recognised plain-text calendar payload or fail visibly."""
    if _CALENDAR_NO_EVENTS_RE.match(result):
        log.info("[Calendar] explicit empty-calendar response")
        return []
    text_events = _parse_calendar_text(result)
    if text_events:
        log.info("[Calendar] plain-text format parsed: %d event(s) found (no attendee data)", len(text_events))
        return text_events
    log.warning("[Calendar] non-JSON response — first 300 chars: %s", result.strip()[:300])
    raise RuntimeError(
        "[Calendar] returned a non-JSON text response — check logs for the raw format to implement a parser"
    )


def _parse_calendar_result(result: Any) -> list[Any]:
    """Normalise the MCP calendar tool result into a flat list of event dicts."""
    if not isinstance(result, str):
        return _calendar_items(result)
    try:
        return _calendar_items(json.loads(result))
    except json.JSONDecodeError:
        # historic regression: try the known text formats before giving up.
        return _parse_calendar_text_or_raise(result)


def _parse_calendar_text(text: str) -> list[dict[str, Any]]:
    """Parse the plain-text calendar format returned by the google_workspace MCP tool.

    Handles the format::

        Successfully retrieved N events from calendar primary for <email>:
         - "Event Title" (Starts: YYYY-MM-DD, Ends: YYYY-MM-DD)

    Events are returned as minimal dicts with ``summary``, ``start``, ``end``,
    and ``attendees`` keys so downstream code can treat them like JSON events.
    All-day events (date-only start) have no time component; attendees are
    empty because the text format does not include attendee data.

    Returns an empty list when the text does not match the expected format.
    """
    events: list[dict[str, Any]] = []
    # Match lines of the form: - "Title" (Starts: YYYY-MM-DD..., Ends: YYYY-MM-DD...)
    for line in text.splitlines():
        m = _EVENT_LINE_RE.search(line)
        if m:
            start_str = m.group("start").strip()
            end_str = m.group("end").strip()
            # historic regression fix: use dateTime key for time-bearing strings so the
            # all-day filter in fetch_external_meetings works correctly.
            # All-day events use "date" (no T); timed events use "dateTime".
            start_obj: dict[str, str] = {"dateTime": start_str} if "T" in start_str else {"date": start_str}
            end_obj: dict[str, str] = {"dateTime": end_str} if "T" in end_str else {"date": end_str}
            events.append(
                {
                    "summary": m.group("title").strip(),
                    "start": start_obj,
                    "end": end_obj,
                    "attendees": [],
                    "_source": "text-format",
                }
            )
    return events


def _collect_attendee_emails(raw_attendees: list[Any]) -> list[str]:
    """Extract email strings from a mixed-type attendees list."""
    emails: list[str] = []
    for att in raw_attendees:
        if isinstance(att, dict):
            email = att.get("email", "")
            if email:
                emails.append(email)
        elif isinstance(att, str):
            emails.append(att)
    return emails


def fetch_external_meetings(
    session: MCPSession,
    target_date: date,
    internal_domains: set[str],
    user_email: str,
) -> list[dict[str, Any]]:
    """Return today's calendar events that have at least one non-internal attendee."""
    if not user_email:
        raise RuntimeError(
            "user_google_email is required by google_workspace__get_events but could not be "
            "determined. Set FIELDKIT_USER_EMAIL env var or ensure USER env var is set."
        )

    day_start = (
        datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    day_end = (
        datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )

    result = session.call_tool(
        "google_workspace__get_events",
        {
            "user_google_email": user_email,
            "calendar_id": "primary",
            "time_min": day_start,
            "time_max": day_end,
            "max_results": 50,
            "detailed": True,
        },
    )

    events = _parse_calendar_result(result)

    external: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        all_emails = _collect_attendee_emails(ev.get("attendees", []))
        ext_emails = [e for e in all_emails if _email_domain(e) not in internal_domains and _email_domain(e)]
        # implementation note: include events with no attendee metadata if they have a non-empty summary.
        # Text-format events always have a summary (set by _parse_calendar_text), so they
        # pass this guard correctly.  Events with no external attendees AND no summary are
        # skipped (e.g. internal-only calendar noise with no useful display title).
        # All-day events (date-only start) are skipped below — they are personal/calendar blocks.
        is_text_format = ev.get("_source") == "text-format"
        if not ext_emails and not ev.get("summary", "").strip():
            continue
        if is_text_format:
            raw_start = ev.get("start", {})
            # historic regression fix: check the key name (date vs dateTime) rather than the
            # value string content.  Checking "T" not in start_val was unreliable
            # because text-format events always stored times under "date" (fixed in
            # _parse_calendar_text), causing timed meetings to be misclassified as
            # all-day and silently dropped from the brief.
            is_all_day = isinstance(raw_start, dict) and "dateTime" not in raw_start
            if is_all_day:
                continue

        raw_start = ev.get("start", {})
        raw_end = ev.get("end", {})
        start_str = (
            raw_start.get("dateTime", raw_start.get("date", "")) if isinstance(raw_start, dict) else str(raw_start)
        )
        end_str = raw_end.get("dateTime", raw_end.get("date", "")) if isinstance(raw_end, dict) else str(raw_end)

        external.append(
            {
                "title": ev.get("summary", "(no title)"),
                "start": start_str,
                "end": end_str,
                "attendees": all_emails,
                "external_attendees": ext_emails,
            }
        )

    return external


# ---------------------------------------------------------------------------
# Pipeline pulse helpers
# ---------------------------------------------------------------------------


_STAGE_RISK: dict[str, int] = {
    "negotiate": 0,
    "propose": 1,
    "validate": 2,
    "discover": 3,
    "qualify": 4,
    "prospect": 5,
}

# _CLOSED_STAGE_NAMES imported from fieldkit.pursuit.stages — single source of truth.


def _accounts_dir() -> Path:
    return get_fieldkit_home() / "accounts"


def get_latest_pursuit_files(n: int = 5) -> list[Path]:
    """Return the *n* highest-priority active pursuit files across all accounts."""
    today = datetime.now(tz=UTC).date()
    pursuit_files: list[tuple[int, int, float, Path]] = []
    for account_dir in _accounts_dir().iterdir():
        pursuits_dir = account_dir / "pursuits"
        if not pursuits_dir.is_dir():
            continue
        for f in pursuits_dir.glob("*.md"):
            if f.stat().st_size == 0 or f.name in {"gmail-intel.md", "template.md"}:
                continue
            try:
                text = f.read_text(encoding="utf-8")
                stage = ""
                close_date_str = ""
                parsed = parse_frontmatter(text)
                if parsed is not None:
                    fm, _ = parsed
                    stage = str(fm.get("stage", "")).lower()
                    close_date_str = str(fm.get("sf_close_date") or "")
            except Exception:  # noqa: BLE001
                log.warning("Skipping unparseable pursuit %s", f, exc_info=True)
                stage = ""
                close_date_str = ""
            if stage in _CLOSED_STAGE_NAMES:
                continue
            red_flag = 1
            if close_date_str:
                try:
                    cd = date.fromisoformat(close_date_str[:10])
                    if (cd - today).days <= 30:
                        red_flag = 0
                except ValueError:
                    log.debug("Unparseable close_date_str %r — skipping red-flag check", close_date_str, exc_info=True)
            risk = _STAGE_RISK.get(stage, 99)
            mtime = f.stat().st_mtime
            pursuit_files.append((red_flag, risk, -mtime, f))

    pursuit_files.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in pursuit_files[:n]]


def extract_pursuit_summary(pursuit_file: Path) -> dict[str, str] | None:
    """Extract key frontmatter fields from a pursuit file for the pipeline pulse.

    Uses load_pursuit() to parse and validate frontmatter via the canonical
    PursuitFrontmatter model (implementation note, implementation note). Returns None when the file
    cannot be parsed (malformed YAML, missing frontmatter, or schema violation).

    Args:
        pursuit_file: Path to the pursuit .md file.

    Returns:
        Dict of display-ready string fields, or None if the file is unparseable.
    """
    try:
        model, _body, _mtime = load_pursuit(pursuit_file)
    except (ValueError, yaml.YAMLError, ValidationError):
        log.warning("Skipping unparseable pursuit %s", pursuit_file, exc_info=True)
        return None

    account = pursuit_file.parent.parent.name
    pursuit_name = pursuit_file.stem

    # Stage: StrEnum — str() gives the value directly (e.g. "propose").
    # Fall back to sf_stage string if stage is the default sentinel.
    stage = str(model.stage) if model.stage else str(model.sf_stage or "unknown")

    # Monetary: float | None → normalize None to "" for display.
    sf_acv = str(model.sf_acv) if model.sf_acv is not None else str(model.sf_arr or "")

    # Date string: str | None → normalize None to "".
    sf_close = model.sf_close_date or ""

    # last_updated: date | str | None → normalize to str or "".
    last_updated_raw = model.last_updated or model.sf_last_pulled or ""
    last_updated = str(last_updated_raw) if last_updated_raw else ""

    # next_steps: str | None — also guard against the literal string "None"
    # that may appear in legacy files written before model validation was added.
    next_steps_raw = model.sf_next_steps or ""
    next_steps_str = str(next_steps_raw)
    if next_steps_str in ("", "None", "null"):
        next_steps_str = ""
    next_steps = next_steps_str[:120] + "…" if len(next_steps_str) > 120 else next_steps_str

    return {
        "account": account,
        "pursuit": pursuit_name,
        "stage": stage,
        "acv": sf_acv,
        "close_date": sf_close,
        "last_updated": last_updated,
        "next_steps": next_steps,
        "path": str(pursuit_file.relative_to(get_fieldkit_home())),
    }


# ---------------------------------------------------------------------------
# Cross-account signal detection
# ---------------------------------------------------------------------------


_STOPWORDS: frozenset[str] = frozenset(
    {
        "update",
        "updates",
        "security",
        "migration",
        "support",
        "service",
        "services",
        "project",
        "review",
        "meeting",
        "summary",
        "email",
        "emails",
        "thread",
        "threads",
        "recent",
        "activity",
        "weekly",
        "monthly",
        "quarter",
        "status",
        "action",
        "items",
        "notes",
        "general",
        "other",
        "follow",
        "internal",
        "external",
        "current",
        "latest",
        "upcoming",
    }
)

# fieldkit-internal vocabulary that appears as section headers and field names
# in every account markdown file.  These words are meaningless as cross-account
# signals because they originate from the tool's own document structure rather
# than from account-specific business activity.
#
# historic regression: Extended with ~37 common business nouns that appear in virtually
# every account's gmail-intel.md and produce false-positive cross-account
# signals (e.g. "contract", "enterprise", "deployment").
CROSS_ACCOUNT_STOPLIST: frozenset[str] = frozenset(
    {
        "contacts",
        "contact",
        "pursuit",
        "pursuits",
        "champion",
        "champions",
        "signals",
        "signal",
        "matches",
        "match",
        "renewal",
        "renewals",
        "checklist",
        "checklists",
        "volume",
        "stage",
        "stages",
        "score",
        "scores",
        "account",
        "accounts",
        "meeting",
        "meetings",
        "notes",
        "summary",
        "update",
        "updates",
        "status",
        "health",
        "risk",
        "opportunity",
        "opportunities",
        "close",
        "date",
        "next",
        "steps",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "have",
        "been",
        "will",
        "was",
        "not",
        "but",
        "all",
        "can",
        "fieldkit",
        "meddpicc",
        "crm",
        # historic regression: generic business vocabulary added below
        "contract",
        "contracts",
        "enterprise",
        "deployment",
        "solution",
        "solutions",
        "platform",
        "platforms",
        "services",
        "service",
        "support",
        "client",
        "clients",
        "team",
        "teams",
        "project",
        "projects",
        "vendor",
        "partner",
        "partners",
        "program",
        "programs",
        "management",
        "manager",
        "director",
        "executive",
        "process",
        "processes",
        "system",
        "systems",
        "review",
        "reports",
        "integration",
        "security",
        "technical",
        "business",
        # historic regression: operator-specific and generic action words surfaced as false signals
        "messages",
        "message",
        "call",
        "calls",
        "check",
        "list",
        "verify",
        "discuss",
        "sync",
        "connect",
        "follow",
        "email",
        "emails",
        "sent",
        "received",
        "last",
        "first",
        "cross",
        "deal",
        "deals",
        # Tool / platform names that are not customer context
        "google",
        "gemini",
        "slack",
        "zoom",
        # Common operator names / org tokens — not customer signals
        "matt",
        "rate",
        "outbound",
        "inbound",
        "reactive",
        "primarily",
        "rarely",
        "initiates",
        "responds",
        "searched",
        "keywords",
        "internal",
    }
)

# Combined stoplist: generic English words + fieldkit-internal vocabulary.
_ALL_STOPWORDS: frozenset[str] = _STOPWORDS | CROSS_ACCOUNT_STOPLIST

_HEADING_RE = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z]{4,}")
_ALERT_HEADING_RE = re.compile(r"^(## \d{4}-\d{2}-\d{2}.*)$", re.MULTILINE)
_EVENT_LINE_RE = re.compile(r'-\s+"(?P<title>[^"]+)"\s+\(Starts:\s+(?P<start>[^,)]+),\s+Ends:\s+(?P<end>[^)]+)\)')


def _is_valid_signal_keyword(word: str) -> bool:
    """Return True if *word* is a meaningful cross-account signal keyword.

    Rejects words that are:
    - in the combined stoplist (generic English or fieldkit-internal vocabulary)
    - shorter than 4 characters (already excluded by _WORD_RE, guard kept for safety)
    - purely numeric
    """
    if word in _ALL_STOPWORDS:
        return False
    if len(word) < 4:
        return False
    return not word.isdigit()


def _internal_account_slugs() -> set[str]:
    """Return account slugs intentionally excluded from shared intelligence."""
    accounts_config = get_accounts_config().get("accounts", {})
    if not isinstance(accounts_config, dict):
        return set()
    return {slug for slug, config in accounts_config.items() if isinstance(config, dict) and config.get("internal")}


def _eligible_signal_keywords(text: str, slug: str) -> set[str]:
    """Return repeated, non-identity keywords from one account's intelligence."""
    identity_tokens = set(_WORD_RE.findall(slug.lower()))
    words = (word for line in text.splitlines() if not line.startswith("#") for word in _WORD_RE.findall(line.lower()))
    counts = Counter(word for word in words if _is_valid_signal_keyword(word) and word not in identity_tokens)
    return {word for word, count in counts.items() if count >= 2}


def _add_account_signal_keywords(
    keyword_to_accounts: dict[str, set[str]], data_root: Path, slug: str, internal_slugs: set[str]
) -> None:
    """Add one eligible account's repeated keywords to the shared index."""
    if slug in internal_slugs:
        log.debug("Cross-account scan: skipping internal account %s", slug)
        return
    intel_path = data_root / "accounts" / slug / "gmail-intel.md"
    log.debug("Cross-account scan: checking %s", intel_path)
    if not intel_path.exists():
        log.debug("Cross-account scan: %s not found, skipping", intel_path)
        return
    try:
        text = intel_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Cross-account scan: could not read %s: %s", intel_path, exc)
        return
    for word in _eligible_signal_keywords(text, slug):
        keyword_to_accounts.setdefault(word, set()).add(slug)


def detect_cross_account_signals(data_root: Path) -> list[dict[str, Any]]:
    """Scan gmail-intel.md files across accounts and return shared topic keywords.

    historic regression: A word must appear at least twice in a single account's file before
    it contributes to that account's signal set.  This prevents one-off mentions
    that happen to appear in two different account files from being treated as
    shared intelligence.
    """
    keyword_to_accounts: dict[str, set[str]] = {}
    internal_slugs = _internal_account_slugs()
    for slug in get_account_names():
        _add_account_signal_keywords(keyword_to_accounts, data_root, slug, internal_slugs)

    signals = [{"topic": kw, "accounts": sorted(accts)} for kw, accts in keyword_to_accounts.items() if len(accts) >= 2]
    signals.sort(key=lambda s: len(s["accounts"]), reverse=True)

    if signals:
        log.info("Cross-account signals found: %d keyword(s) across 2+ accounts", len(signals))
    else:
        log.debug("Cross-account scan: no shared signals detected")

    return signals


__all__ = [
    "CROSS_ACCOUNT_STOPLIST",
    "_ALL_STOPWORDS",
    "_CALENDAR_NO_EVENTS_RE",
    "_HEADING_RE",
    "_STAGE_RISK",
    "_STOPWORDS",
    "_WORD_RE",
    "MCPSession",
    "_accounts_dir",
    "_collect_attendee_emails",
    "_email_domain",
    "_is_valid_signal_keyword",
    "_parse_calendar_result",
    "_parse_calendar_text",
    "_parse_internal_domains",
    "detect_cross_account_signals",
    "extract_pursuit_summary",
    "extract_today_alerts",
    "fetch_external_meetings",
    "get_latest_pursuit_files",
    "resolve_user_email",
]
