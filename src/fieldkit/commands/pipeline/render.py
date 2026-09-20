"""Rendering functions and rules engine for the pipeline review.

Contains table rendering, section rendering helpers, and the narrative LLM
prompt builder.
"""

from datetime import UTC, date, datetime
from typing import Any

from fieldkit.commands.pipeline.collect import (
    GATE_ICONS,
    STAGE_ORDER,
    ChampionSignal,
    PursuitRow,
)
from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE, wrap_user_data
from fieldkit.pursuit.stages import CLOSED_STAGES


def _quarter_label(d: date) -> str:
    """Return 'QN YYYY' label for a date (e.g. 'Q2 2026')."""
    q = (d.month - 1) // 3 + 1
    return f"Q{q} {d.year}"


# ── Table rendering ───────────────────────────────────────────────────────────


def render_pipeline_table(rows: list[PursuitRow]) -> str:
    """Render deterministic Markdown table sorted by stage order, then days desc.

    Includes an ``Avg Days`` column showing average days per stage transition
    (implementation change: renamed from ``Vel`` for clarity).
    ``—`` when transition_history is absent from the frontmatter.

    Returns the full Markdown table as a string (no trailing newline).
    """
    if not rows:
        return (
            "| Account | Deal | Stage | Gate | Days | Avg Days | Close | Native Qualification |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| — | No active pursuits | — | — | — | — | — | — |"
        )

    def sort_key(r: PursuitRow) -> tuple[int, int]:
        stage_ord = STAGE_ORDER.get(r.stage, 99)
        days = r.days_in_stage if r.days_in_stage >= 0 else 0
        return (stage_ord, -days)

    sorted_rows = sorted(rows, key=sort_key)

    header = "| Account | Deal | Stage | Gate | Days | Avg Days | Close | Native Qualification |"
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [header, sep]

    today = datetime.now(tz=UTC).date()
    for r in sorted_rows:
        gate_icon = GATE_ICONS.get(r.gate_status, "🔲")
        days_str = str(r.days_in_stage) if r.days_in_stage >= 0 else "?"
        # Velocity: show one decimal place when available, "—" otherwise.
        vel_str = f"{r.velocity_days_per_stage:.1f}" if r.velocity_days_per_stage is not None else "—"
        # Format close date with urgency flags
        close_display = "—"
        if r.close_date_str:
            try:
                close_date = date.fromisoformat(r.close_date_str[:10])
                days_to_close = (close_date - today).days
                if close_date < today and r.stage not in CLOSED_STAGES:
                    close_display = f"🔴 {r.close_date_str[:10]}"  # overdue
                elif days_to_close <= 14:
                    close_display = f"🚨 {r.close_date_str[:10]}"  # historic regression: ≤14 days: critical
                elif days_to_close <= 30:
                    close_display = f"🔴 {r.close_date_str[:10]}"  # ≤30 days: urgent
                elif days_to_close <= 60:
                    close_display = f"🟡 {r.close_date_str[:10]}"  # 31-60 days: watch
                else:
                    close_display = r.close_date_str[:10]  # >60 days: plain
            except ValueError:
                close_display = r.close_date_str[:10]
        lines.append(
            f"| {r.account} | {r.deal} | {r.stage} | {gate_icon} {r.gate_status} | {days_str} | {vel_str} "
            f"| {close_display} | {r.native_qualification} |"
        )

    return "\n".join(lines)


# ── Section renderers ─────────────────────────────────────────────────────────


def _render_champion_section(signals: list[ChampionSignal] | None) -> str:
    # historic regression: None means gmail.db was unavailable; [] means db present but no matches
    if signals is None:
        return "_Champion stats unavailable — gmail.db not found. Run `fieldkit gmail sync` to build the database._"
    if not signals:
        return "_No active pursuits have both a derived champion identity and matching Gmail signal data._"
    lines = ["| Account | Deal | Champion | Last Outbound | Signal |", "|---|---|---|---|---|"]
    for s in signals:
        lines.append(
            f"| {s['account']} | {s['deal']} | {s['champion']} | {s['last_outbound'] or '—'} | {s['signal'] or '—'} |"
        )
    return "\n".join(lines)


# User-facing label is "Pursuit Coverage" (historic regression/#806): the check measures whether
# each account has enough active pursuits, distinct from the CRM-visibility "blindspot"
# concept surfaced by `gmail backstory-gap`. Internal symbol names and the
# `blindspot_threshold` accounts.yaml key are retained to avoid a breaking config change.
def _render_blindspot_section(blindspots: list[dict[str, Any]]) -> str:
    issues = [b for b in blindspots if b["status"] == "blindspot"]
    if not issues:
        return "_All accounts meet their pursuit-coverage threshold._"
    lines = ["| Account | Active Pursuits | Threshold |", "|---|---|---|"]
    for b in issues:
        lines.append(f"| {b['account']} | {b['active_pursuits']} | {b['threshold']} |")
    return "\n".join(lines)


def _build_narrative_prompt(
    rows: list[PursuitRow],
    champion_signals: list[ChampionSignal] | None,  # historic regression: None when gmail.db absent
    blindspot_data: list[dict[str, Any]],
    today: date,
) -> str:
    """Build the LLM prompt for narrative synthesis."""
    table = render_pipeline_table(rows)
    champion_section = _render_champion_section(champion_signals)
    blindspot_section = _render_blindspot_section(blindspot_data)
    # historic regression: wrap all user-controlled sections in <user_data> delimiters before
    # interpolation. table/champion_section/blindspot_section embed CRM data (account
    # names, contact names, pursuit titles) that participants could control.
    table_safe = wrap_user_data(table, "pipeline_table")
    champion_safe = wrap_user_data(champion_section, "champion_signals")
    blindspot_safe = wrap_user_data(blindspot_section, "pursuit_coverage")
    return f"""{UNTRUSTED_DATA_PREAMBLE}

You are reviewing a consulting pipeline for {today.isoformat()}.

## Pursuit Health
{table_safe}

## Champion Signals
{champion_safe}

## Pursuit Coverage
{blindspot_safe}

Write a concise narrative summary (under 200 words) covering:
1. Overall pipeline health and momentum
2. Deals requiring immediate attention (stalled, gate failures, native qualification pending or unavailable)
3. Champion/relationship risks
4. Recommended top 3 actions for this week
"""
