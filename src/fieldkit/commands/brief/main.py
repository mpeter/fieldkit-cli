"""Morning brief generator — orchestrator.

Wires together the data collectors (collect.py) and renderers (render.py)
with the LLM synthesis call, then writes the final brief to disk.

Usage:
    python -m fieldkit.commands.brief [--no-llm] [--account ACCOUNT]

--no-llm:  Skip the Claude LLM synthesis call; write collector output
           directly with a placeholder for email/calendar/priorities.
--account: Limit the whole brief (pursuit alerts, signals, and the Project
           Health section) to a single account slug.
"""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import click

from fieldkit.commands.brief.collect import (
    _gmail_db_exists,
    collect_champion_signals,
    collect_decay_signals,
    collect_pursuit_alerts,
    collect_tasks,
)
from fieldkit.commands.brief.render import (
    _render_degraded_section,
    _render_no_llm_brief,
)
from fieldkit.config import ConfigError, get_config_path, get_fieldkit_home, get_fieldkit_root
from fieldkit.errors import LLMError
from fieldkit.llm import LLM_SYNTHESIS_TIMEOUT, synthesize
from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE, wrap_user_data
from fieldkit.provenance import derived_doc_marker
from fieldkit.pursuit import iterate_pursuits
from fieldkit.util.atomic import assert_nonzero_write

log = logging.getLogger(__name__)

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
    """Write the stamped brief, preserving the implementation note empty-output guard.

    The provenance marker alone must never mask an empty brief: stamping empty
    content would produce a non-zero file that defeats ``assert_nonzero_write``.
    An empty body is therefore written unstamped so the guard still raises
    RuntimeError and deletes the stub (same observable contract as before
    implementation change).
    """
    output_path.write_text(_stamped(brief) if brief.strip() else "", encoding="utf-8")
    # implementation note: post-write size guard — delete stub and raise on 0-byte output.
    assert_nonzero_write(output_path)


def _collect_degraded_sources(data_root: Path) -> list[tuple[str, str]]:
    """Return (label, reason) tuples for unavailable data sources.

    Defined here (not in collect.py) so that tests can monkeypatch
    ``main._gmail_db_exists`` and have this function see the patched version
    (Python name lookup is module-scoped at call time).
    """
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
    """Run stale-prose-check.py across all pursuit directories.

    The script path is derived from this module's location so it is always
    found relative to the installed package, regardless of the fieldkit_root
    config value.  Fixes historic regression (Two-Root Convention violation).

    The script path is resolved from the configured fieldkit root.
    """
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


def _brief_save_notice(output_path: Path, *, dry_run: bool) -> str:
    """Return the human-readable persistence notice, if the brief was written."""
    return "" if dry_run else f"\n\nBrief saved: {output_path}"


def _persist_brief(output_dir: Path, output_path: Path, brief: str, *, dry_run: bool) -> None:
    """Write a brief unless this invocation is an explicitly non-mutating preview."""
    if dry_run:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_brief(output_path, brief)


def _emit_brief_result(
    *, as_json: bool, brief: str, output_path: Path, account: str | None, degraded: bool, dry_run: bool = False
) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "account": account,
                    "degraded": degraded,
                    "dry_run": dry_run,
                    "path": None if dry_run else str(output_path),
                    "written": not dry_run,
                },
                sort_keys=True,
            )
        )
    else:
        click.echo(brief + _brief_save_notice(output_path, dry_run=dry_run))


def _run(
    no_llm: bool, account: str | None, verbose: bool = False, *, as_json: bool = False, dry_run: bool = False
) -> None:
    """Core logic for the morning brief generator."""
    # Resolve data_root (user data repo) and output_dir separately so that a
    # missing config only degrades the brief output location, not data lookups.
    # Fixes historic regression: the old fallback silently substituted the code repo as
    # data_root, causing collect_tasks() to look for TASKS.md in the wrong place.
    data_root: Path | None
    try:
        data_root = get_fieldkit_home()
        output_dir = data_root / "briefs"
    except ConfigError as exc:
        # historic regression: --no-llm must save to data_root/briefs/ (the same path that
        # `brief open` reads).  When config is absent we cannot determine that
        # path, so exit 3 (configuration error) rather than silently writing to
        # the fallback location that `brief open` will never find.
        if no_llm:
            # Raise ConfigError so cli_main() maps it to EXIT_DATA (3).
            # Raising here instead of calling sys.exit() directly keeps the
            # exit-code contract: only cli_main() at the entry point calls sys.exit().
            raise ConfigError(
                f"Cannot save brief: data root is unavailable ({exc}). Configure fieldkit first with 'fieldkit init'."
            ) from exc
        click.echo(
            f"[morning_brief] Config unavailable ({exc}); data collectors will degrade gracefully.",
            err=True,
        )
        data_root = None
        # Fall back to a temp-style location under the code repo for the brief file only.
        output_dir = get_fieldkit_root() / "briefs"

    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"

    click.echo(f"[morning_brief] Collecting data for {today_str} ...", err=True)

    if data_root is not None:
        _t = time.monotonic()
        pursuit_alerts = collect_pursuit_alerts(data_root, account)
        if verbose:
            click.echo(f"[brief] collect_pursuit_alerts... done ({time.monotonic() - _t:.2f}s)", err=True)

        _t = time.monotonic()
        champion_signals = collect_champion_signals(data_root, account)
        if verbose:
            click.echo(f"[brief] collect_champion_signals... done ({time.monotonic() - _t:.2f}s)", err=True)

        _t = time.monotonic()
        decay_signals = collect_decay_signals(data_root, account)
        if verbose:
            click.echo(f"[brief] collect_decay_signals... done ({time.monotonic() - _t:.2f}s)", err=True)

        _t = time.monotonic()
        stale_prose = collect_stale_prose(data_root)
        if verbose:
            click.echo(f"[brief] collect_stale_prose... done ({time.monotonic() - _t:.2f}s)", err=True)

        _t = time.monotonic()
        tasks_today, tasks_waiting = collect_tasks(data_root)
        if verbose:
            click.echo(f"[brief] collect_tasks... done ({time.monotonic() - _t:.2f}s)", err=True)

        degraded_sources = _render_degraded_section(_collect_degraded_sources(data_root))
    else:
        pursuit_alerts = "Data root unavailable — pursuit alerts skipped."
        champion_signals = "Data root unavailable — champion signals skipped."
        decay_signals = "Data root unavailable — decay signals skipped."
        stale_prose = "Data root unavailable — stale prose check skipped."
        tasks_today = "(config unavailable — cannot read TASKS.md)"
        tasks_waiting = "(config unavailable — cannot read TASKS.md)"
        degraded_sources = _render_degraded_section([("All data collectors", "config unavailable")])

    if no_llm:
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
        _emit_brief_result(
            as_json=as_json, brief=brief, output_path=output_path, account=account, degraded=False, dry_run=dry_run
        )
        return

    # LLM synthesis path — send pre-computed data to synthesize()
    # historic regression/historic regression: wrap ALL user-controlled data in <user_data> delimiters to prevent prompt injection.
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
> Email data is not yet collected automatically. Run `fieldkit gmail inbox`
> to review overnight emails.

### 📅 Today's Calendar
> Calendar data is not yet integrated. Check your calendar directly.

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

    try:
        brief = synthesize(prompt, timeout=LLM_SYNTHESIS_TIMEOUT)
        if not brief or not brief.strip():
            raise LLMError("synthesize() returned empty response — falling back to no-LLM mode")
    except LLMError as exc:
        click.echo(f"ERROR: LLM synthesis failed: {exc}", err=True)
        # historic regression: prepend a visible DEGRADED banner so the brief content itself
        # signals degradation when opened in a Markdown viewer (where stderr is
        # invisible).  Exit code 1 lets agents and CI detect the degraded run
        # without parsing stderr.  Per AGENTS.md exit taxonomy: code 1 = partial
        # failure (retry may help).
        degraded_banner = (
            "> [DEGRADED] LLM synthesis failed — this brief was generated without AI synthesis."
            " Re-run without --no-llm when the model is available.\n\n"
            f"> Error: {exc}\n\n"
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
        brief = degraded_banner + brief
        _persist_brief(output_dir, output_path, brief, dry_run=dry_run)
        _emit_brief_result(
            as_json=as_json, brief=brief, output_path=output_path, account=account, degraded=True, dry_run=dry_run
        )
        # Raise LLMError(category="rate-limit") so cli_main() maps it to EXIT_PARTIAL (1).
        # Raising here instead of calling sys.exit(1) directly keeps the exit-code
        # contract: only cli_main() at the entry point calls sys.exit().
        raise LLMError(str(exc), category="rate-limit", original=exc) from exc

    _persist_brief(output_dir, output_path, brief, dry_run=dry_run)
    _emit_brief_result(
        as_json=as_json, brief=brief, output_path=output_path, account=account, degraded=False, dry_run=dry_run
    )


# ── Feature introspection metadata (consumed by fieldkit version --features) ──

DESCRIPTION = "Generate today's pipeline-only brief — pursuit alerts, champion signals, decay, and priorities"
USAGE = "fieldkit brief generate --pipeline-only [--no-llm] [--account ACCOUNT] [--verbose]"
FLAGS = {
    "--no-llm": "Skip LLM synthesis; write collector output directly",
    "--account": "Limit brief to a single account slug",
    "--verbose / -v": "Print per-collector timing to stderr",
}
