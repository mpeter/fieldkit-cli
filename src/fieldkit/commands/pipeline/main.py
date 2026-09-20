"""Pipeline review orchestrator.

Assembles collect/render/quota modules and exposes render_full_brief + _run.

Usage:
    python -m fieldkit.commands.pipeline [--no-llm]

--no-llm:  Skip LLM synthesis; write deterministic table with placeholder
           for narrative summary.
"""

import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import click

from fieldkit.cli_exit import EXIT_DATA
from fieldkit.commands.pipeline.collect import (
    ChampionSignal,
    PursuitRow,
    collect_all_pursuit_data,
)
from fieldkit.commands.pipeline.render import (
    _build_narrative_prompt,
    _quarter_label,
    _render_blindspot_section,
    _render_champion_section,
    render_pipeline_table,
)
from fieldkit.config import ConfigError, get_fieldkit_home
from fieldkit.errors import LLMError
from fieldkit.llm import synthesize

log = logging.getLogger(__name__)
_SAFE_ACCOUNT_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")


def render_full_brief(
    rows: list[PursuitRow],
    *,
    champion_signals: list[ChampionSignal] | None,
    blindspot_data: list[dict[str, Any]],
    no_llm: bool,
    today: date,
) -> str:
    """Compose the complete pipeline review Markdown document."""
    quarter = _quarter_label(today)
    table = render_pipeline_table(rows)
    champion_section = _render_champion_section(champion_signals)
    blindspot_section = _render_blindspot_section(blindspot_data)

    if no_llm:
        llm_block = "_[LLM narrative skipped — run without --no-llm for synthesis]_"
    else:
        prompt = _build_narrative_prompt(rows, champion_signals, blindspot_data, today)
        try:
            llm_block = synthesize(prompt, timeout=60)  # 60s — pipeline is time-sensitive (historic regression)
        except LLMError as exc:
            # historic regression/376: sanitise error — never embed LiteLLM/Vertex class names or
            # model identifiers in the rendered brief. Full error logged at WARNING only.
            log.warning("LLM narrative synthesis failed: %s", exc, exc_info=True)
            llm_block = "_LLM synthesis unavailable — pipeline review generated without AI narrative._"

    return f"""# Pipeline Review — {today.isoformat()} ({quarter})

## Pursuit Health Table

{table}

## Champion Signals

{champion_section}

## Pursuit Coverage

{blindspot_section}

## Narrative Summary

{llm_block}
"""


def _run(no_llm: bool, data_root_override: Path | None, account: str | None = None) -> None:
    """Core logic for the pipeline review generator."""
    if account is not None and _SAFE_ACCOUNT_SLUG_RE.fullmatch(account) is None:
        click.echo(f"ERROR: unsafe account slug '{account}'.", err=True)
        raise SystemExit(EXIT_DATA)

    try:
        data_root = data_root_override or get_fieldkit_home()
    except ConfigError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None

    today = datetime.now(tz=UTC).date()
    output_dir = data_root / "briefs"
    output_dir.mkdir(parents=True, exist_ok=True)
    account_component = f"{account}-" if account is not None else ""
    output_path = output_dir / f"pipeline-review-{account_component}{today.isoformat()}.md"

    rows, champion_signals, blindspot_data = collect_all_pursuit_data(data_root, account_filter=account)

    brief = render_full_brief(
        rows=rows,
        champion_signals=champion_signals,
        blindspot_data=blindspot_data,
        no_llm=no_llm,
        today=today,
    )

    output_path.write_text(brief, encoding="utf-8")
    click.echo(brief)
    click.echo(f"\n---\nSaved to: {output_path}", err=True)
