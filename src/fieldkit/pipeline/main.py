"""Pipeline review orchestrator.

Assembles collect/render/quota modules and exposes render_full_brief + _run.

Usage:
    fieldkit pipeline [--no-llm]

--no-llm:  Skip LLM synthesis; write the deterministic pipeline sections.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import get_llm_model, llm_disabled
from fieldkit.errors import LLMError
from fieldkit.llm import synthesize
from fieldkit.pipeline.collect import (
    ChampionSignal,
    PursuitRow,
    collect_all_pursuit_data,
)
from fieldkit.pipeline.render import (
    _build_narrative_prompt,
    _quarter_label,
    _render_blindspot_section,
    _render_champion_section,
    render_pipeline_table,
)
from fieldkit.util.atomic import assert_nonzero_write, atomic_text_write, require_nonempty_output


def render_full_brief(
    rows: list[PursuitRow],
    *,
    champion_signals: list[ChampionSignal] | None,
    blindspot_data: list[dict[str, Any]],
    no_llm: bool,
    today: date,
) -> str:
    """Compose a review; provider failures propagate with their original category."""
    quarter = _quarter_label(today)
    table = render_pipeline_table(rows)
    champion_section = _render_champion_section(champion_signals)
    blindspot_section = _render_blindspot_section(blindspot_data)

    narrative = ""
    if not (no_llm or llm_disabled() or get_llm_model() is None):
        prompt = _build_narrative_prompt(rows, champion_signals, blindspot_data, today)
        llm_block = synthesize(prompt, timeout=60)
        if not llm_block.strip():
            raise LLMError("Model returned an empty pipeline narrative.")
        narrative = f"\n## Narrative Summary\n\n{llm_block}\n"

    return f"""# Pipeline Review — {today.isoformat()} ({quarter})

## Pursuit Health Table

{table}

## Champion Signals

{champion_section}

## Pursuit Coverage

{blindspot_section}
{narrative}
"""


@dataclass(frozen=True)
class PipelineReviewResult:
    text: str
    path: Path
    provider_failure: LLMError | None


def generate_review(*, no_llm: bool, data_root: Path, account: str | None = None) -> PipelineReviewResult:
    """Collect and persist a review, retaining provider failure for the CLI boundary."""
    if account is not None and re.fullmatch(r"[A-Za-z0-9_-]+", account) is None:
        raise ValueError("unsafe account slug")
    today = datetime.now(tz=UTC).date()
    output_dir = data_root / "briefs"
    account_component = f"{account}-" if account is not None else ""
    output_path = output_dir / f"pipeline-review-{account_component}{today.isoformat()}.md"

    rows, champion_signals, blindspot_data = collect_all_pursuit_data(data_root, account_filter=account)

    failure: LLMError | None = None
    try:
        brief = render_full_brief(
            rows=rows,
            champion_signals=champion_signals,
            blindspot_data=blindspot_data,
            no_llm=no_llm,
            today=today,
        )
    except LLMError as exc:
        failure = exc
        fallback = render_full_brief(
            rows=rows,
            champion_signals=champion_signals,
            blindspot_data=blindspot_data,
            no_llm=True,
            today=today,
        )
        require_nonempty_output(fallback)
        brief = (
            "> [DEGRADED] LLM synthesis failed — this review contains deterministic pipeline sections only."
            " Check model configuration and credentials before retrying.\n\n"
        ) + fallback

    require_nonempty_output(brief)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_text_write(output_path, brief)
    assert_nonzero_write(output_path)
    return PipelineReviewResult(brief, output_path, failure)
