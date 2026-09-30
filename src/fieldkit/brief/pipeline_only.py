"""Collect, render, and optionally synthesize a local pipeline-only brief.

The command adapter owns CLI output and exit handling. A dry run renders from
local inputs without calling a model or writing a report.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fieldkit.brief.collect import (
    _gmail_db_exists,
    collect_champion_signals,
    collect_decay_signals,
    collect_pursuit_alerts,
    collect_tasks,
)
from fieldkit.brief.render import (
    _render_degraded_section,
    _render_no_llm_brief,
)
from fieldkit.config import ConfigError, get_config_path, get_fieldkit_home, get_llm_model, llm_disabled
from fieldkit.errors import LLMError
from fieldkit.llm import LLM_SYNTHESIS_TIMEOUT, synthesize
from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE, wrap_user_data
from fieldkit.provenance import derived_doc_marker
from fieldkit.pursuit import iterate_pursuits
from fieldkit.util.atomic import assert_nonzero_write, atomic_text_write, require_nonempty_output

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineOnlyResult:
    text: str
    path: Path
    account: str | None
    degraded: bool
    dry_run: bool
    provider_failure: LLMError | None
    timings: tuple[tuple[str, float], ...]


# implementation change: provenance marker — the brief is a derived summary, not a system of record.
_BRIEF_MARKER = derived_doc_marker(
    caste="summary",
    derived_from=["gmail.db", "TASKS.md", "pursuit frontmatter (accounts/)", "watcher alert state"],
    generated_by="fieldkit brief generate --pipeline-only",
)


def _stamped(brief: str) -> str:
    """Prepend the derived-doc provenance marker to the brief content for storage.

    Applied at every write site so the stored artifact (and any copy of it)
    carries its caste; the on-screen echo stays unstamped.
    """
    return _BRIEF_MARKER + "\n" + brief


def _write_brief(output_path: Path, brief: str) -> None:
    """Publish a complete stamped brief without replacing prior output with an empty body."""
    require_nonempty_output(brief)
    atomic_text_write(output_path, _stamped(brief))
    assert_nonzero_write(output_path)


def _collect_degraded_sources(data_root: Path) -> list[tuple[str, str]]:
    """Return (label, reason) tuples for unavailable data sources."""
    degraded: list[tuple[str, str]] = []

    if not _gmail_db_exists():
        degraded.append(("Gmail cache", "gmail.db not found"))

    pursuits_dir = data_root / "accounts"
    if not pursuits_dir.exists():
        degraded.append(("Pursuits", "accounts directory not found"))

    accounts_yaml = get_config_path("accounts.yaml")
    if not accounts_yaml.exists():
        degraded.append(("Accounts config", "accounts.yaml not found"))

    return degraded


def collect_stale_prose(data_root: Path) -> str:
    """Check workspace pursuits using the installed stale-prose checker."""
    from fieldkit.pursuit.stale import check_file

    pursuit_files = list(iterate_pursuits(data_root))
    if not pursuit_files:
        return "None detected."

    all_warnings: list[str] = []
    for path in pursuit_files:
        all_warnings.extend(check_file(str(path)))

    if not all_warnings:
        return "None detected."
    return "\n".join(all_warnings)


# ── Entry Point ───────────────────────────────────────────────────────────────


def _persist_brief(output_dir: Path, output_path: Path, brief: str, *, dry_run: bool) -> None:
    """Write a brief unless this invocation is an explicitly non-mutating preview."""
    require_nonempty_output(brief)
    if dry_run:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_brief(output_path, brief)


def generate_pipeline_only(*, no_llm: bool, account: str | None, dry_run: bool = False) -> PipelineOnlyResult:
    """Generate a local pipeline brief and retain any provider failure for the CLI."""
    try:
        data_root = get_fieldkit_home()
    except ConfigError as exc:
        raise ConfigError(
            f"Cannot generate brief: workspace is unavailable ({exc}). Configure fieldkit first with 'fieldkit init'."
        ) from exc

    output_dir = data_root / "briefs"
    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"

    timings: list[tuple[str, float]] = []

    _t = time.monotonic()
    pursuit_alerts = collect_pursuit_alerts(data_root, account)
    timings.append(("collect_pursuit_alerts", time.monotonic() - _t))

    _t = time.monotonic()
    champion_signals = collect_champion_signals(data_root, account)
    timings.append(("collect_champion_signals", time.monotonic() - _t))

    _t = time.monotonic()
    decay_signals = collect_decay_signals(data_root, account)
    timings.append(("collect_decay_signals", time.monotonic() - _t))

    _t = time.monotonic()
    stale_prose = collect_stale_prose(data_root)
    timings.append(("collect_stale_prose", time.monotonic() - _t))

    _t = time.monotonic()
    tasks_today, tasks_waiting = collect_tasks(data_root)
    timings.append(("collect_tasks", time.monotonic() - _t))

    degraded_sources = _render_degraded_section(_collect_degraded_sources(data_root))

    if dry_run or no_llm or llm_disabled() or get_llm_model() is None:
        brief = _render_no_llm_brief(
            today_str=today_str,
            pursuit_alerts=pursuit_alerts,
            stale_prose=stale_prose,
            tasks_today=tasks_today,
            tasks_waiting=tasks_waiting,
            champion_signals=champion_signals,
            decay_signals=decay_signals,
            degraded_sources=degraded_sources,
        )
        _persist_brief(output_dir, output_path, brief, dry_run=dry_run)
        return PipelineOnlyResult(brief, output_path, account, False, dry_run, None, tuple(timings))

    # LLM synthesis path — send pre-computed data to synthesize()
    # Wrap all user-controlled data before including it in the model prompt.
    _pursuit_alerts_safe = wrap_user_data(pursuit_alerts, "pursuit_alerts")
    _stale_prose_safe = wrap_user_data(stale_prose, "stale_prose")
    _tasks_today_safe = wrap_user_data(tasks_today, "tasks_today")
    _tasks_waiting_safe = wrap_user_data(tasks_waiting, "tasks_waiting")
    # historic regression: champion/decay signals are user-controlled (from gmail.db contact names and signal
    # labels). Wrap them — previously these were interpolated raw.
    _champion_signals_safe = wrap_user_data(champion_signals, "champion_signals")
    _decay_signals_safe = wrap_user_data(decay_signals, "decay_signals")

    prompt = f"""{UNTRUSTED_DATA_PREAMBLE}

You are a productivity assistant for account engineering work.

Today is {today_str}.

GLOBAL FORMAT RULE: Never use markdown tables anywhere in this brief. No pipe characters (|), no table headers, no --- row separators. Use bullet lists for all structured data.

Produce the morning pipeline brief. Structure:

## ☀️ Morning Brief — {today_str}

Display these sections verbatim — do not add, remove, or modify them:

### 📬 Overnight Email Triage
> Overnight email is not collected by this account brief. Review your mail directly.

### 📅 Today's Calendar
> Calendar events are not collected by this account brief. Review your calendar directly.

### 🚦 Pursuit Alerts
The following pursuit issues were detected by automated frontmatter scan.
Do NOT re-scan files — use this pre-computed list verbatim, then add a brief
recommended action for each flagged item based on what you know about the deal:

{_pursuit_alerts_safe}

### 📄 Stale Prose Warnings
These pursuit files have body text that contradicts their Salesforce frontmatter.
Display these verbatim — they need manual cleanup by the AE:

{_stale_prose_safe}

### 📋 Today's Commitments
These are already committed for today from TASKS.md — display them verbatim:

{_tasks_today_safe}

### ⏳ Waiting On
These items are pending a response from others — flag any with an overdue follow-up date:

{_tasks_waiting_safe}

### 🤝 Relationship Signals

#### Champion Health
Pre-computed from gmail.db — do NOT re-query Gmail.

{_champion_signals_safe}

For any champion showing REACTIVE or COLD signal on a deal in propose/close stage,
flag it with a one-line action (e.g. "schedule a check-in with [name] before next
customer meeting").

#### Relationship Decay (domain-filtered, ≥10 messages, silent >60d)
Pre-computed from gmail.db — do NOT re-query Gmail.

{_decay_signals_safe}

Identify any contact in the "cooling" or COLD range who is named in an active
pursuit. Flag with a one-line action. Skip contacts you don't recognize.

### 🎯 Top 3 Priorities Today
Based on Today's Commitments above, pursuit alerts, calendar, emails, and
relationship signals — recommend the 3 most important things to accomplish.
If Today's Commitments are already well-chosen, affirm them. Be specific —
not 'follow up with Global Pay' but 'send follow-up on Project Shift timeline to
[name] re: yesterday's steering committee'.

Keep the entire brief under 550 words.
"""

    failure: LLMError | None = None
    try:
        brief = synthesize(prompt, timeout=LLM_SYNTHESIS_TIMEOUT)
        if not brief or not brief.strip():
            raise LLMError("Model returned an empty brief.")
    except LLMError as exc:
        failure = exc
        degraded_banner = (
            "> [DEGRADED] LLM synthesis failed — this brief was generated without AI synthesis."
            " Check model configuration and credentials before retrying.\n\n"
        )
        brief = _render_no_llm_brief(
            today_str=today_str,
            pursuit_alerts=pursuit_alerts,
            stale_prose=stale_prose,
            tasks_today=tasks_today,
            tasks_waiting=tasks_waiting,
            champion_signals=champion_signals,
            decay_signals=decay_signals,
            degraded_sources=degraded_sources,
        )
        require_nonempty_output(brief)
        brief = degraded_banner + brief
    _persist_brief(output_dir, output_path, brief, dry_run=dry_run)
    return PipelineOnlyResult(brief, output_path, account, failure is not None, dry_run, failure, tuple(timings))
