"""Rendering and orchestration for the morning brief watcher."""

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypedDict

from fieldkit.companion.outbox import list_proposals
from fieldkit.config import get_fieldkit_data, get_fieldkit_home, get_pipeline_quota
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.stage_weights import STAGE_WEIGHTS
from fieldkit.watch._morning_brief_types import SourceNotReady

log = logging.getLogger(__name__)


# historic regression: Stage probability weights are now imported from the shared
# stage_weights module. Previously QUOTA_STAGE_WEIGHTS used higher values
# (negotiate=0.90, propose=0.60, etc.) which caused pipeline quota and
# pursuit forecast to report different weighted numbers for the same pursuits.
# The correct values are the more conservative forecast.py ones.
QUOTA_STAGE_WEIGHTS: dict[str, float] = STAGE_WEIGHTS


class QuotaGapResult(TypedDict):
    """Return type of calculate_quota_gap."""

    target: float
    closed_won: float
    weighted: float
    gap: float
    excluded_count: int  # historic regression: number of pursuits skipped due to missing ACV
    excluded_names: list[str]  # historic regression: names of excluded pursuits


def _parse_sf_amount(raw: object) -> float | None:
    """Parse an sf_arr/sf_acv/sf_consulting_acv value into a float.

    Returns None when value is absent or not parseable (so callers can skip
    the pursuit with a debug log).
    """
    if raw is None:
        return None
    s = str(raw).strip().lstrip("$").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def calculate_quota_gap(
    pursuits: list[dict[str, object]],
    quota_config: dict[str, object],
) -> QuotaGapResult:
    """Calculate quota gap from a list of pursuit dicts and quota config.

    Each pursuit dict must have:
      - ``stage``: str matching CLOSED_STAGES or QUOTA_STAGE_WEIGHTS keys
      - ``sf_amount``: numeric or dollar-string amount (sf_consulting_acv / sf_acv / sf_arr)

    Pursuits missing ``sf_amount`` (None or unparseable) are skipped with debug log.

    Args:
        pursuits: List of dicts, each with at least ``stage`` and ``sf_amount`` keys.
        quota_config: Dict with ``target`` key (numeric, the quota target).

    Returns:
        Dict with keys:
          - ``target``: quota target
          - ``closed_won``: sum of closed-won pursuit amounts
          - ``weighted``: probability-weighted sum of active pursuit amounts
          - ``gap``: target - closed_won - weighted
    """
    target = float(quota_config.get("target") or 0)  # type: ignore[arg-type]
    closed_won = 0.0
    weighted = 0.0

    excluded_names: list[str] = []
    for p in pursuits:
        stage = str(p.get("stage", ""))
        raw_amount = p.get("sf_amount")
        amount = _parse_sf_amount(raw_amount)
        if amount is None:
            name = str(p.get("name", p.get("stage", "unknown")))
            log.debug("Skipping pursuit (missing sf_amount): name=%s stage=%s", name, stage)
            excluded_names.append(name)
            continue

        if stage == Stage.CLOSED_WON:
            closed_won += amount
        elif stage in QUOTA_STAGE_WEIGHTS or p.get("sf_probability") is not None:
            sf_prob = p.get("sf_probability")
            if sf_prob is not None:
                weighted += amount * float(str(sf_prob)) / 100.0
            else:
                weighted += amount * QUOTA_STAGE_WEIGHTS.get(stage, 0.0)

    gap = target - closed_won - weighted
    return QuotaGapResult(
        target=target,
        closed_won=closed_won,
        weighted=weighted,
        gap=gap,
        excluded_count=len(excluded_names),
        excluded_names=excluded_names,
    )


# ---------------------------------------------------------------------------
# Degraded sources helpers
# ---------------------------------------------------------------------------


def _collect_degraded_sources(sources: dict[str, list[Any] | SourceNotReady | str]) -> list[tuple[str, str]]:
    """Return (label, reason) tuples for any source whose result is a string (error).

    Sources that return a list are healthy and are excluded from the result.
    SourceNotReady instances are also excluded — they indicate an optional watcher
    that simply hasn't run yet, which is NOT a failure (implementation note).
    """
    degraded: list[tuple[str, str]] = []
    for label, result in sources.items():
        # implementation note: SourceNotReady is not a str, so isinstance(result, str) already
        # excludes it.  The explicit guard below documents intent and future-proofs
        # against accidental str subclassing.
        if isinstance(result, SourceNotReady):
            continue
        if isinstance(result, str):
            reason = result.strip().strip("_").strip()
            if "unavailable:" in reason:
                reason = reason.split("unavailable:", 1)[1].strip()
            reason = reason[:100] or "unavailable"
            degraded.append((label, reason))
    return degraded


def _render_degraded_section(degraded: list[tuple[str, str]]) -> list[str]:
    """Render a '## Degraded Sources' markdown section."""
    if not degraded:
        return []
    lines: list[str] = ["## Degraded Sources", ""]
    for label, reason in degraded:
        lines.append(f"- **{label}**: {reason}")
    lines += ["", "---", ""]
    return lines


# ---------------------------------------------------------------------------
# Brief rendering helpers
# ---------------------------------------------------------------------------


def _fmt_time(dt_str: str) -> str:
    """Format an ISO datetime string to HH:MM, or return as-is on parse failure."""
    if not dt_str:
        return ""
    try:
        normalised = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        return dt.strftime("%H:%M")
    except ValueError:
        return dt_str


def _render_alert_blocks(blocks: list[str] | SourceNotReady | str, empty_msg: str) -> list[str]:
    """Render a list of alert blocks, a SourceNotReady sentinel, or an error string.

    SourceNotReady is rendered the same as a plain str (its message is the display
    text), but it is NOT counted as a source failure (implementation note).
    """
    if isinstance(blocks, SourceNotReady):
        return [blocks.message]
    if isinstance(blocks, str):
        return [blocks]
    if not blocks:
        return [empty_msg]
    lines: list[str] = []
    for block in blocks:
        lines += [block, ""]
    return lines


def _render_meetings_section(meetings: list[dict[str, Any]] | str) -> list[str]:
    lines = ["## Today's External Meetings", ""]
    if isinstance(meetings, str):
        # historic regression: render the error string directly so the AE sees what failed.
        # The string already contains the sanitised error text from _collect_calendar_meetings.
        log.debug("Calendar section: rendering error string: %s", meetings)
        lines.append(meetings)
    elif not meetings:
        lines.append("_No external meetings found for today._")
    else:
        for ev in meetings:
            start = _fmt_time(ev.get("start", ""))
            end = _fmt_time(ev.get("end", ""))
            time_range = f"{start}–{end}" if start and end else start or end or ""  # noqa: RUF001
            ext = ", ".join(ev.get("external_attendees", []))
            lines.append(f"- **{ev['title']}** {time_range}")
            if ext:
                lines.append(f"  - External: {ext}")
    lines += ["", "---", ""]
    return lines


_ACCT_RE = re.compile(r"\*\*Account:\*\*\s*`([^`]+)`")


def _strip_alert_headings(block: str) -> str:
    """Remove date headings and duplicate section headings from alert block text.

    Draft-queue alert files include ``## YYYY-MM-DD`` date headings and
    ``### Pending Outbox`` subheadings that leak into the brief as visible
    content when the block body is appended verbatim. This helper strips them
    so only the payload (draft count, draft items) reaches the rendered brief.

    historic regression
    """
    filtered = [
        line
        for line in block.splitlines()
        if not re.match(r"^## \d{4}-\d{2}-\d{2}", line) and line.strip() != "### Pending Outbox"
    ]
    return "\n".join(filtered).strip()


def _render_stall_blocks_grouped(alerts: list[str] | SourceNotReady | str) -> list[str]:
    """Render pursuit stall alert blocks grouped by account."""
    if isinstance(alerts, (SourceNotReady, str)) or not alerts:
        return _render_alert_blocks(alerts, "_No pursuit stall alerts today._")

    by_account: dict[str, list[str]] = {}
    for block in alerts:
        m = _ACCT_RE.search(block)
        account = m.group(1) if m else "other"
        by_account.setdefault(account, []).append(block)

    lines: list[str] = []
    for account in sorted(by_account):
        blocks = by_account[account]
        lines.append(f"**{account}** ({len(blocks)} stall(s))")
        lines.append("")
        for block in blocks:
            lines.append(block)
            lines.append("")
    return lines


def _render_alerts_section(
    backstory_alerts: list[str] | SourceNotReady | str,
    pursuit_stall_alerts: list[str] | SourceNotReady | str,
    slack_alerts: list[str] | SourceNotReady | str,
    contract_expiry_alerts: list[str] | SourceNotReady | str | None = None,
    draft_queue_alerts: list[str] | SourceNotReady | str | None = None,
) -> list[str]:
    lines = ["## Account Alerts", ""]
    lines += ["### Backstory Health", ""]
    lines += _render_alert_blocks(backstory_alerts, "_No Backstory alerts today._")
    lines += ["", "### Pursuit Stalls", ""]
    lines += _render_stall_blocks_grouped(pursuit_stall_alerts)
    lines += ["", "### Slack Threads", ""]
    lines += _render_alert_blocks(slack_alerts, "_No Slack thread alerts today._")
    if contract_expiry_alerts is not None:
        lines += ["", "### Contract Expiry", ""]
        lines += _render_alert_blocks(contract_expiry_alerts, "_No contract expiry alerts today._")
    if draft_queue_alerts is not None:
        lines += ["", "### Pending Outbox", ""]
        # historic regression: strip date headings and duplicate "### Pending Outbox" headings
        # that are baked into draft-queue alert blocks by extract_today_alerts().
        cleaned_drafts = (
            [_strip_alert_headings(b) for b in draft_queue_alerts]
            if isinstance(draft_queue_alerts, list)
            else draft_queue_alerts
        )
        lines += _render_alert_blocks(cleaned_drafts, "_No stale drafts today._")
    lines += ["", "---", ""]
    return lines


def _render_cross_account_section(signals: list[dict[str, Any]]) -> list[str]:
    """Render a '## Cross-Account Signals' markdown section."""
    if not signals:
        return []
    lines = ["## Cross-Account Signals", ""]
    for sig in signals:
        accounts_str = ", ".join(sig["accounts"])
        lines.append(f"- **{sig['topic']}** — {accounts_str}")
    lines += ["", "---", ""]
    return lines


def _render_pipeline_section(pursuit_summaries: list[dict[str, str]] | str) -> list[str]:
    lines = ["## Pipeline Pulse (5 highest-priority pursuits)", ""]
    if isinstance(pursuit_summaries, str):
        lines.append(pursuit_summaries)
    elif not pursuit_summaries:
        lines.append("_No pursuit files found._")
    else:
        today = datetime.now(tz=UTC).date()
        for ps in pursuit_summaries:
            acv_val = ps["acv"]
            try:
                acv_fmt = f"${float(acv_val):,.0f}"
            except (ValueError, TypeError):
                acv_fmt = acv_val
            acv = f" · ACV {acv_fmt}" if acv_val else ""
            close_str = ps["close_date"]
            red = ""
            if close_str:
                try:
                    cd = date.fromisoformat(close_str[:10])
                    if (cd - today).days <= 30:
                        red = " 🔴"
                except ValueError:
                    pass
            close = f" · close {close_str}{red}" if close_str else ""
            lines.append(f"- **{ps['account']} / {ps['pursuit']}** — stage: `{ps['stage']}`{acv}{close}")
            if ps["next_steps"]:
                lines.append(f"  - Next steps: {ps['next_steps']}")
            if ps["last_updated"]:
                lines.append(f"  - Last updated: {ps['last_updated']}")
    return lines


def _project_health_lines(healths: list[str], account: str | None, matched_any: bool) -> list[str]:
    if account and not matched_any:
        return [
            "## Project Health",
            "",
            f"_No projects found for account `{account}` — check the account slug._",
            "",
        ]
    counts = {health: healths.count(health) for health in ("ZOMBIE", "EXPIRING", "SOON")}
    parts = [
        f"{icon} {counts[health]} {health}"
        for health, icon in (("ZOMBIE", "☠"), ("EXPIRING", "✗"), ("SOON", "⚠"))
        if counts[health]
    ]
    if not parts:
        return []
    # historic regression: the footer in render_brief() already provides the separator.
    return [
        "## Project Health",
        "",
        f"**{' | '.join(parts)}** — run `fieldkit pursuit projects` for details.",
        "",
    ]


def _render_project_health_section(account: str | None = None) -> list[str]:
    """Append an account-scoped project health summary when action is needed."""
    try:
        from fieldkit.pursuit.projects import classify_project
    except ImportError:
        log.debug("Project health section unavailable: projects_health import failed", exc_info=True)
        return []

    try:
        data_root = get_fieldkit_home()
        today = datetime.now(tz=UTC).date()
        accounts_dir = data_root / "accounts"
        healths: list[str] = []
        matched_any = False
        for path in sorted(accounts_dir.glob("*/projects/*.md")):
            if account and path.parts[-3].lower() != account.lower():
                continue
            matched_any = True
            row = classify_project(path, accounts_dir, today)
            if row is not None:
                healths.append(row.health)
        return _project_health_lines(healths, account, matched_any)
    except Exception:  # noqa: BLE001
        # F2 (historic regression follow-up): malformed data and I/O failures are visible.
        log.warning("Project health section unavailable", exc_info=True)
        return []


def _render_quota_section(
    data_root: Path | None, quota_collector: Callable[[Path], list[dict[str, object]]]
) -> list[str]:
    """Render the quota gap section for the morning brief."""
    quota_config = get_pipeline_quota()
    if not quota_config:
        return [
            "## Quota Gap",
            "",
            "_No quota configured. Run: `fieldkit pipeline quota --set <target> --period <period>`_",
            "",
            "---",
            "",
        ]

    try:
        root = data_root or get_fieldkit_home()
    except (OSError, ValueError, KeyError):
        return [
            "## Quota Gap",
            "",
            "_[SOURCE] unavailable: could not read data root_",
            "",
            "---",
            "",
        ]

    pursuits = quota_collector(root)
    result = calculate_quota_gap(pursuits, quota_config)

    def _fmt(v: float) -> str:
        return f"${v:,.0f}"

    gap = result["gap"]
    gap_str = _fmt(gap) if gap >= 0 else f"-{_fmt(abs(gap))}"
    period = quota_config.get("period", "")
    period_label = f" ({period})" if period else ""

    return [
        f"## Quota Gap{period_label}",
        "",
        "| Metric | Amount |",
        "|--------|--------|",
        f"| Target | {_fmt(result['target'])} |",
        f"| Closed-won | {_fmt(result['closed_won'])} |",
        f"| Weighted pipeline | {_fmt(result['weighted'])} |",
        f"| Gap | {gap_str} |",
        "",
        "---",
        "",
    ]


def _render_companion_outbox_pointer() -> list[str]:
    """One-line pointer when companion proposals await review (propose tier).

    Optional by design (agent-companion-loop D4): silent when the outbox
    directory is absent or empty.
    """
    try:
        data_path = get_fieldkit_data()
        pending = list_proposals(data_path)
    except OSError:
        return []
    if not pending:
        return []
    return [f"**{len(pending)} companion proposal(s) pending review** — {data_path / 'companion-outbox'}", ""]


def render_brief(
    target_date: date,
    *,
    meetings: list[dict[str, Any]] | str,
    backstory_alerts: list[str] | SourceNotReady | str,
    pursuit_stall_alerts: list[str] | SourceNotReady | str,
    slack_alerts: list[str] | SourceNotReady | str,
    pipeline_review_md: str,
    elapsed_seconds: float,
    quota_collector: Callable[[Path], list[dict[str, object]]],
    contract_expiry_alerts: list[str] | SourceNotReady | str | None = None,
    draft_queue_alerts: list[str] | SourceNotReady | str | None = None,
    data_root: Path | None = None,
    cross_account_signals: list[dict[str, Any]] | None = None,
    degraded_sources: list[tuple[str, str]] | None = None,
    account: str | None = None,
) -> str:
    """Render the morning brief markdown document.

    ``account``, when set, scopes the Project Health section to that account.
    ``cross_account_signals`` / ``_render_cross_account_section`` are deliberately
    NOT scoped — cross-account signal detection is cross-account by design.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    header = [
        f"# Morning Brief — {date_str}",
        "",
        f"_Generated at {generated_at} · duration {elapsed_seconds:.1f}s_",
        "",
        "---",
        "",
    ]
    footer = [
        "",
        "---",
        "",
        f"<!-- generated: {generated_at} | duration: {elapsed_seconds:.1f}s -->",
        "",
    ]

    pipeline_lines = ["## Pipeline Review", "", pipeline_review_md, "", "---", ""]
    cross_account_lines = _render_cross_account_section(cross_account_signals or [])
    degraded_lines = _render_degraded_section(degraded_sources or [])

    body = (
        degraded_lines
        + _render_meetings_section(meetings)
        + _render_alerts_section(
            backstory_alerts, pursuit_stall_alerts, slack_alerts, contract_expiry_alerts, draft_queue_alerts
        )
        + cross_account_lines
        + pipeline_lines
        + _render_quota_section(data_root, quota_collector)
        + _render_project_health_section(account=account)
        + _render_companion_outbox_pointer()
    )

    return "\n".join(header + body + footer)
