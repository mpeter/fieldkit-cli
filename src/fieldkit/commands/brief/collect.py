"""Data-collection functions for the morning brief.

Each function gathers a single category of data from local sources
(pursuit files, gmail.db, TASKS.md) and returns a formatted Markdown string
(or a tuple of strings for collect_tasks).
"""

import contextlib
import io
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from fieldkit.config import ConfigError
from fieldkit.gmail.decay_domain import decay_report
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.exceptions import GmailDbNotFoundError
from fieldkit.gmail.query_domain import connect as _gmail_connect
from fieldkit.gmail.query_domain import query_champion_signals
from fieldkit.pursuit import (
    CLOSED_STAGES,
    calculate_days_since,
    extract_champion_name,
    iterate_pursuits,
    read_accounts_config,
)
from fieldkit.pursuit.io import load_pursuit
from fieldkit.pursuit.qualification import native_qualification_status

_CLOSED_STAGES = CLOSED_STAGES
_PIPELINE_PULSE_TOP_N: int = 5
_PIPELINE_PULSE_RED_DAYS: int = 30  # close date ≤ this many days → RED priority
log = logging.getLogger(__name__)


def _gmail_db_exists() -> bool:
    """Return True if gmail.db is reachable via the configured data root."""
    try:
        db = get_gmail_db_path()
        return db.exists()
    except ConfigError:
        return False


# ── Collectors ────────────────────────────────────────────────────────────────


def collect_pursuit_alerts(data_root: Path, account_filter: str | None = None) -> str:
    """Return pursuit alerts plus the honest current native qualification state."""
    blocks: list[str] = []

    for path in iterate_pursuits(data_root):
        account = path.parts[-3]
        if account_filter and account.lower() != account_filter.lower():
            continue

        try:
            model, _body, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            log.debug("Skipping invalid pursuit file %s", path, exc_info=True)
            continue  # skip invalid pursuit

        if model.stage in _CLOSED_STAGES:
            continue

        pursuit = path.stem
        alerts: list[str] = []

        # Stage age > 14 days (matches fieldkit watch pursuit-stalls default threshold).
        if model.last_transition:
            days = calculate_days_since(str(model.last_transition))
            if days > 14:
                alerts.append(f"⏱ Stuck in **{model.stage}** for {days} days (since {model.last_transition})")
        else:
            # historic regression: surface pursuits with no transition date so stall duration
            # is not silently skipped — the AE cannot know how long it has been
            # in this stage without an explicit flag.
            alerts.append(f"⏱ No last_transition date recorded — stall duration unknown for {model.stage} stage")

        alerts.append(f"Native qualification: {native_qualification_status(model.sf_opportunity_id)}")
        if model.gate_status == "override":
            alerts.append("⚠️ Explicit stage-gate override recorded")

        if alerts:
            lines = [f"**{account} / {pursuit}** (stage: {model.stage})"]
            lines.extend(f"  - {a}" for a in alerts)
            blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "No active pursuits flagged today."


def collect_pipeline_pulse(data_root: Path, account_filter: str | None = None) -> str:
    """Return a Markdown block of the top-5 active pursuits by urgency.

    Sort key: RED close-date (≤30 days) first, then soonest close date. Pursuits
    with no close date sort last. Historical local qualification never ranks deals.

    Returns at most _PIPELINE_PULSE_TOP_N (5) pursuits.
    """
    today = datetime.now(tz=UTC).date()

    # Collect (sort_key, display_line) tuples for all active pursuits
    entries: list[tuple[tuple[int, int, str], str]] = []

    for path in iterate_pursuits(data_root):
        account = path.parts[-3]
        if account_filter and account.lower() != account_filter.lower():
            continue

        try:
            model, _body, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            log.debug("Skipping invalid pursuit file %s", path, exc_info=True)
            continue

        if model.stage in _CLOSED_STAGES:
            continue

        # Determine days-to-close for RED classification
        days_to_close: int | None = None
        close_str = ""
        if model.sf_close_date:
            try:
                close_str = str(model.sf_close_date).strip()
                # Handle M/D/YYYY and YYYY-MM-DD formats
                if "/" in close_str:
                    parts = close_str.split("/")
                    if len(parts) == 3:
                        m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                        close_date = date(y, m, d)
                        days_to_close = (close_date - today).days
                else:
                    close_date = date.fromisoformat(close_str[:10])
                    days_to_close = (close_date - today).days
            except (ValueError, TypeError):
                log.debug("Unparseable sf_close_date %r in %s", close_str, path)

        # Keep urgency/date ordering independent from historical qualification.
        is_red = 0 if (days_to_close is not None and days_to_close <= _PIPELINE_PULSE_RED_DAYS) else 1
        close_sort = days_to_close if days_to_close is not None else 10**9
        sort_key = (is_red, close_sort, str(path))

        # Build display line
        close_info = f", closes in {days_to_close}d" if days_to_close is not None else ""
        red_flag = " 🔴" if is_red == 0 else ""
        qualification = native_qualification_status(model.sf_opportunity_id)
        line = f"**{account} / {path.stem}** (stage: {model.stage}{close_info}; native qualification: {qualification}){red_flag}"
        entries.append((sort_key, line))

    if not entries:
        return "No active pursuits found."

    # Sort by close-date urgency and take top N.
    entries.sort(key=lambda e: e[0])
    top = entries[:_PIPELINE_PULSE_TOP_N]
    lines = [f"{i + 1}. {line}" for i, (_, line) in enumerate(top)]
    return "\n".join(lines)


def _champion_signal_block(
    conn: sqlite3.Connection,
    data_root: Path,
    account: str,
    path: Path,
) -> str | None:
    """Return a formatted signal block for a single pursuit, or None to skip."""
    try:
        model, _body, _ = load_pursuit(path)
    except (ValueError, yaml.YAMLError, ValidationError):
        log.debug("Skipping invalid pursuit file %s", path, exc_info=True)
        return None

    if model.stage in _CLOSED_STAGES:
        return None

    account_dir = data_root / "accounts" / account
    champion_name = extract_champion_name(account_dir)
    if not champion_name:
        return None

    try:
        output = query_champion_signals(conn, champion_name)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        log.warning("Error querying champion signals for %s: %s", champion_name, exc, exc_info=True)
        return None

    if not output.strip():
        return None

    signal_lines = [
        line
        for line in output.splitlines()
        if any(kw in line for kw in ("Threads initiated", "Last outbound", "Signal:"))
    ][:3]
    if not signal_lines:
        return None

    pursuit = path.stem
    lines = [f"**{account} / {pursuit}** — champion: {champion_name}"]
    lines.extend(f"  {line}" for line in signal_lines)
    return "\n".join(lines)


def collect_champion_signals(data_root: Path, account_filter: str | None = None) -> str:
    """Query gmail.db for champion contact signals on active pursuits."""
    db_path = get_gmail_db_path() if _gmail_db_exists() else None
    if db_path is None:
        return "gmail.db not found — champion signals unavailable."

    blocks: list[str] = []

    try:
        conn = _gmail_connect(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, GmailDbNotFoundError) as exc:
        log.warning("Cannot open gmail.db for champion signals: %s", exc, exc_info=True)
        return "gmail.db unavailable — champion signals unavailable."

    try:
        for path in iterate_pursuits(data_root):
            account = path.parts[-3]
            if account_filter and account.lower() != account_filter.lower():
                continue
            block = _champion_signal_block(conn, data_root, account, path)
            if block:
                blocks.append(block)
    finally:
        conn.close()

    return "\n\n".join(blocks) if blocks else "No champion signal data available."


def collect_decay_signals(data_root: Path, account_filter: str | None = None) -> str:
    """Run decay_report() per account and return formatted decay report."""
    db_path = get_gmail_db_path() if _gmail_db_exists() else None
    if db_path is None:
        return "gmail.db not found — decay signals unavailable."

    try:
        config = read_accounts_config(data_root)
    except (FileNotFoundError, yaml.YAMLError):
        return "accounts.yaml not found — decay signals unavailable."

    accounts_raw = config.get("accounts", {})
    accounts_section: dict[str, object] = accounts_raw if isinstance(accounts_raw, dict) else {}
    blocks: list[str] = []

    try:
        conn = _gmail_connect(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, GmailDbNotFoundError) as exc:
        log.warning("Cannot open gmail.db for decay signals: %s", exc, exc_info=True)
        return "gmail.db unavailable — decay signals unavailable."

    try:
        for acct_name, acct_data in accounts_section.items():
            if account_filter and acct_name.lower() != account_filter.lower():
                continue
            if not isinstance(acct_data, dict):
                continue
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    decay_report(
                        conn,
                        account=acct_name,
                        days_threshold=60,
                        show_all=False,
                        domain_filter=None,  # Use account tag — works for all domains
                        min_messages=10,
                        limit=10,
                    )
                output = buf.getvalue().strip()
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                log.warning("Error running decay report for %s: %s", acct_name, exc, exc_info=True)
                output = "(error running decay report)"
            blocks.append(f"**{acct_name}**\n{output}")
    finally:
        conn.close()

    return "\n\n".join(blocks) if blocks else "No decay data available."


def collect_tasks(data_root: Path) -> tuple[str, str]:
    """Extract Today and Waiting On sections from TASKS.md in data_root."""
    tasks_file = data_root / "TASKS.md"
    if not tasks_file.exists():
        return "(nothing committed for today yet)", "(nothing in the queue)"

    text = tasks_file.read_text(encoding="utf-8")

    def _extract_section(section_header: str) -> str | None:
        """Extract up to 10 non-comment, non-blank lines from a named section in TASKS.md.

        Returns:
            str: joined lines if the section exists and has non-comment content.
            "":  the section heading was found but all content lines were blank or HTML comments.
                 Callers can distinguish "section exists but empty" from "section not present".
            None: the section heading was not found in the file at all.

        Note:
            Matches any Markdown heading level (# through ######) case-insensitively (implementation note).
            Stops at the next heading of any level to prevent bleed-in from adjacent sections.
        """
        lines = text.splitlines()
        in_section = False
        found_section = False
        result: list[str] = []
        for line in lines:
            # implementation note: match any heading level (# through ######) case-insensitively.
            # Previously only ## was matched, so single-# headings in TASKS.md were silently skipped.
            if re.match(r"^#{1,6}\s+" + re.escape(section_header), line, re.IGNORECASE):
                in_section = True
                found_section = True
                continue
            # implementation note: stop at any heading level, not just ##.
            # Previously `line.startswith("## ")` allowed content from the next section
            # to bleed in when the next section used a different heading level.
            if in_section and re.match(r"^#{1,6}\s+", line):
                break
            if in_section and line.strip() and not line.startswith("<!--"):
                result.append(line)
        items = result[:10]
        if items:
            return "\n".join(items)
        # implementation note: if the section was found but all lines were HTML comments (or blank),
        # return "" (empty but found) rather than None so callers can distinguish
        # "section exists but empty" from "section not present at all".
        if found_section:
            return ""
        return None

    today = _extract_section("Today") or "(nothing committed for today yet)"
    waiting = _extract_section("Waiting On") or "(nothing in the queue)"
    return today, waiting
