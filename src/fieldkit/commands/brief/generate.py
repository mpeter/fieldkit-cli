"""Morning brief generator — orchestrator (moved from commands/watch/morning_brief.py).

Business logic that does NOT depend on fieldkit.commands.pipeline lives in
``fieldkit.watch.morning_brief`` and ``fieldkit.watch.morning_brief_collect``.

Functions that MUST stay in the commands layer (tach boundary: fieldkit.watch
cannot depend on fieldkit.commands):
  - _collect_pipeline_review (uses fieldkit.commands.pipeline.*)
  - _run_generate_inner (calls _collect_pipeline_review)
  - _run_generate (wraps _run_generate_inner)

Writes the merged brief (alerts + calendar + pipeline review) to
<fieldkit_home>/briefs/ — the same directory `brief open` reads and the
pipeline-only path (main.py:_run) already writes to.

Implementation is split across submodules:
    watch/morning_brief_mcp.py             — MCPSession JSON-RPC client (domain)
    watch/morning_brief_collect.py         — data collection helpers (domain)
    watch/morning_brief_render.py          — markdown rendering (domain)
    watch/morning_brief.py                 — path helpers + alert collection (domain)
    commands/brief/generate.py             — pipeline review + orchestrator (here)
"""

import json
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import click

from fieldkit.commands.pipeline.collect import collect_all_pursuit_data
from fieldkit.commands.pipeline.main import render_full_brief as _render_pipeline_full_brief
from fieldkit.commands.pipeline.quota import _collect_pursuits_for_quota
from fieldkit.config import get_accounts_config, get_config_path, get_fieldkit_home
from fieldkit.watch._morning_brief_types import SourceNotReady
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.morning_brief import (
    _backstory_alerts_file,
    _collect_alert_source,
    _collect_calendar_meetings,
    _collect_degraded_sources,
    _contract_expiry_alerts_file,
    _draft_queue_alerts_file,
    _pursuit_stall_alerts_file,
    _slack_alerts_file,
    _write_brief_to_disk,
)
from fieldkit.watch.morning_brief_collect import (
    _parse_internal_domains,
    detect_cross_account_signals,
    resolve_user_email,
)
from fieldkit.watch.morning_brief_render import render_brief

log = logging.getLogger("fieldkit.watch.morning_brief.generate")


# ---------------------------------------------------------------------------
# Pipeline review (stays in commands — uses fieldkit.commands.pipeline.*)
# ---------------------------------------------------------------------------


def _collect_pipeline_review(*, no_llm: bool, account: str | None = None) -> str:
    """Collect all pursuit data and render the full pipeline review; return error string on failure."""
    try:
        data_root = get_fieldkit_home()
        rows, champion_signals, blindspot_data = collect_all_pursuit_data(data_root, account_filter=account)
        log.info("Pipeline review: %d pursuit rows collected", len(rows))
        return _render_pipeline_full_brief(
            rows,
            champion_signals=champion_signals,
            blindspot_data=blindspot_data,
            no_llm=no_llm,
            today=datetime.now(tz=UTC).date(),
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"[Pipeline Review] unavailable: {exc}"
        log.warning(msg, exc_info=True)
        return msg


# ---------------------------------------------------------------------------
# Core orchestrator (stays in commands — calls _collect_pipeline_review)
# ---------------------------------------------------------------------------


def _run_generate(
    *,
    date_str: str | None,
    dry_run: bool,
    verbose: bool,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Core logic; returns POSIX exit code."""
    with watcher_logging("morning-brief"):
        return _run_generate_inner(
            date_str=date_str, dry_run=dry_run, verbose=verbose, account=account, as_json=as_json
        )


def _run_generate_inner(
    *,
    date_str: str | None,
    dry_run: bool,
    verbose: bool,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Inner logic (separated for testability); returns POSIX exit code."""
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            log.error("Invalid --date value %r — expected YYYY-MM-DD", date_str, exc_info=True)
            return 1
    else:
        target_date = datetime.now(tz=UTC).date()

    log.info("Morning brief watcher starting — target date: %s", target_date.strftime("%Y-%m-%d"))
    start = time.monotonic()

    config = get_accounts_config()
    if not config:
        log.error("Config load failed: accounts.yaml missing or invalid at %s", get_config_path("accounts.yaml"))
        return 1

    internal_domains = _parse_internal_domains(config)
    user_email = resolve_user_email(config)

    backstory_result = _collect_alert_source(_backstory_alerts_file(), target_date, "Backstory alerts", dry_run=dry_run)
    # historic regression: when dry-run, the alert file hasn't been updated yet — label it
    _stalls_not_found_msg = (
        "_No pursuit stall data yet — run `fieldkit watch run pursuit-stalls` first._"
        if not dry_run
        else "_(dry-run: reading from existing alert file — run without --dry-run for live stall count)_"
    )
    pursuit_stall_result = _collect_alert_source(
        _pursuit_stall_alerts_file(),
        target_date,
        "Pursuit Stalls",
        not_found_msg=_stalls_not_found_msg,
        dry_run=dry_run,
    )
    # implementation change: when the stall file exists but is >72h old (3x the 24h watcher cadence),
    # replace results with a staleness note so the brief clearly signals stale data
    # rather than silently showing 0 blocks or an outdated block list.
    if not dry_run:
        _stall_file = _pursuit_stall_alerts_file()
        if _stall_file.exists():
            _age_hours = (time.time() - _stall_file.stat().st_mtime) / 3600
            if _age_hours > 72:
                pursuit_stall_result = [
                    f"_Stall data is {int(_age_hours)}h old — run `fieldkit watch run pursuit-stalls` to refresh._"
                ]
    slack_result = _collect_alert_source(
        _slack_alerts_file(),
        target_date,
        "Slack",
        not_found_msg="_No Slack thread data yet — run `fieldkit watch run slack-threads` first._",
        dry_run=dry_run,
    )
    contract_expiry_result = _collect_alert_source(
        _contract_expiry_alerts_file(),
        target_date,
        "Contract Expiry",
        not_found_msg="_No contract expiry data yet — run `fieldkit watch run contract-expiry` first._",
        dry_run=dry_run,
    )
    draft_queue_result = _collect_alert_source(
        _draft_queue_alerts_file(),
        target_date,
        "Draft Queue",
        not_found_msg="_No draft queue data yet — run `fieldkit watch run draft-queue` first._",
        dry_run=dry_run,
    )
    meetings_result = _collect_calendar_meetings(target_date, internal_domains, user_email)
    pipeline_review_result = _collect_pipeline_review(no_llm=dry_run, account=account)
    cross_account_signals = detect_cross_account_signals(get_fieldkit_home())

    # Detect pipeline review failure by checking for the error prefix.
    # _collect_pipeline_review() always returns a str (markdown on success, error on failure),
    # so we cannot use isinstance(result, str) to distinguish the two cases.
    # Only count it as degraded when it starts with the error sentinel prefix.
    _PIPELINE_ERROR_PREFIX = "[Pipeline Review] unavailable:"
    pipeline_review_failed = pipeline_review_result.startswith(_PIPELINE_ERROR_PREFIX)

    all_sources: dict[str, list[Any] | SourceNotReady | str] = {
        "calendar": meetings_result,
        "backstory": backstory_result,
        "pursuit stalls": pursuit_stall_result,
        "slack": slack_result,
        "contract expiry": contract_expiry_result,
        "draft queue": draft_queue_result,
        # Pass the error string only on failure so _collect_degraded_sources counts it;
        # pass an empty list (healthy sentinel) on success so it is excluded.
        "pipeline review": pipeline_review_result if pipeline_review_failed else [],
    }
    degraded = _collect_degraded_sources(all_sources)

    elapsed = time.monotonic() - start
    brief_md = render_brief(
        target_date=target_date,
        meetings=meetings_result,
        backstory_alerts=backstory_result,
        pursuit_stall_alerts=pursuit_stall_result,
        slack_alerts=slack_result,
        pipeline_review_md=pipeline_review_result,
        elapsed_seconds=elapsed,
        quota_collector=_collect_pursuits_for_quota,
        contract_expiry_alerts=contract_expiry_result,
        draft_queue_alerts=draft_queue_result,
        cross_account_signals=cross_account_signals,
        degraded_sources=degraded,
        account=account,
    )

    if dry_run:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "account": account,
                        "date": target_date.isoformat(),
                        "degraded_sources": degraded,
                        "dry_run": True,
                        "path": None,
                        "written": False,
                    },
                    sort_keys=True,
                )
            )
        else:
            click.echo(brief_md)
        log.info("Dry run — brief not written to disk (duration: %.1fs)", elapsed)
        return 0

    output_dir = get_fieldkit_home() / "briefs"
    rc = _write_brief_to_disk(
        brief_md,
        target_date,
        elapsed,
        [
            meetings_result,
            backstory_result,
            pursuit_stall_result,
            slack_result,
            contract_expiry_result,
            draft_queue_result,
            # Pass empty list (healthy sentinel) when pipeline review succeeded so
            # _write_brief_to_disk does not count it as a source failure.
            pipeline_review_result if pipeline_review_failed else [],
        ],
        dry_run=dry_run,
        output_dir=output_dir,
    )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "account": account,
                    "date": target_date.isoformat(),
                    "degraded_sources": degraded,
                    "dry_run": False,
                    "path": str(output_dir / f"morning-brief-{target_date.isoformat()}.md"),
                    "written": rc == 0,
                },
                sort_keys=True,
            )
        )
    return rc
