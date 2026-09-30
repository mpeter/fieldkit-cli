"""Collect, render, and publish the merged morning brief."""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import (
    ConfigError,
    get_accounts_config,
    get_fieldkit_home,
    get_llm_model,
    get_mcp_endpoint,
    llm_disabled,
)
from fieldkit.errors import AuthError, LLMError
from fieldkit.pipeline.collect import collect_all_pursuit_data
from fieldkit.pipeline.main import render_full_brief
from fieldkit.pipeline.quota import _collect_pursuits_for_quota
from fieldkit.watch._morning_brief_types import BriefWriteResult, SourceNotReady
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
from fieldkit.watch.status import WatcherRunResult

log = logging.getLogger("fieldkit.watch.morning_brief.generate")


@dataclass(frozen=True)
class MergedBriefResult:
    """Domain result consumed by the CLI and aggregate watcher adapters."""

    run: WatcherRunResult
    account: str | None = None
    target_date: date | None = None
    text: str | None = None
    degraded_sources: list[tuple[str, str]] | None = None
    dry_run: bool = False
    path: Path | None = None
    write_result: BriefWriteResult | None = None
    provider_failure: LLMError | None = None


# ---------------------------------------------------------------------------
# Pipeline review
# ---------------------------------------------------------------------------


def _collect_pipeline_review(
    *, no_llm: bool, account: str | None = None, fallback_review: list[str] | None = None
) -> str:
    """Collect all pursuit data and render the full pipeline review; return error string on failure."""
    try:
        data_root = get_fieldkit_home()
        rows, champion_signals, blindspot_data = collect_all_pursuit_data(data_root, account_filter=account)
        log.info("Pipeline review: %d pursuit rows collected", len(rows))
        today = datetime.now(tz=UTC).date()
        try:
            return render_full_brief(
                rows,
                champion_signals=champion_signals,
                blindspot_data=blindspot_data,
                no_llm=no_llm,
                today=today,
            )
        except LLMError:
            if fallback_review is not None:
                try:
                    fallback_review.append(
                        render_full_brief(
                            rows,
                            champion_signals=champion_signals,
                            blindspot_data=blindspot_data,
                            no_llm=True,
                            today=today,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 -- preserve the original provider error
                    log.warning("Deterministic pipeline review rendering failed (%s)", type(exc).__name__)
                    fallback_review.append("_Pipeline review unavailable after provider failure._")
            raise
    except (AuthError, ConfigError, LLMError):
        raise
    except Exception as exc:  # noqa: BLE001 -- one local source degrades the assembled brief
        msg = "[Pipeline Review] unavailable: local collection failed; retry after checking fieldkit doctor"
        log.warning("Pipeline review collection failed (%s)", type(exc).__name__)
        return msg


# ---------------------------------------------------------------------------
# Merged brief orchestration
# ---------------------------------------------------------------------------


def generate_merged_brief(
    *,
    date_str: str | None,
    dry_run: bool,
    account: str | None = None,
    no_llm: bool | None = None,
    calendar_enabled: bool | None = None,
) -> MergedBriefResult:
    """Build the merged brief without coupling its decisions to CLI output."""
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            log.error("Invalid --date value; expected YYYY-MM-DD")
            return MergedBriefResult(WatcherRunResult("fatal", False, None))
    else:
        target_date = datetime.now(tz=UTC).date()

    log.info("Morning brief watcher starting — target date: %s", target_date.strftime("%Y-%m-%d"))
    start = time.monotonic()

    config = get_accounts_config()
    if not config:
        log.error("Account configuration is missing or invalid")
        return MergedBriefResult(WatcherRunResult("fatal", False, None))

    internal_domains = _parse_internal_domains(config)
    user_email = resolve_user_email(config)

    backstory_configured = get_mcp_endpoint("backstory") is not None
    backstory_result = _collect_alert_source(
        _backstory_alerts_file(),
        target_date,
        "Backstory alerts",
        not_found_msg=(
            "_No Backstory data yet — run `fieldkit watch run backstory-health` first._"
            if backstory_configured
            else "_Backstory input was not run because its endpoint is not configured._"
        ),
        dry_run=dry_run,
    )
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
        not_found_msg=(
            "_Slack input was not run. Select `--slack` on the aggregate command or run the Slack watcher directly._"
        ),
        dry_run=dry_run,
    )
    contract_expiry_result = _collect_alert_source(
        _contract_expiry_alerts_file(),
        target_date,
        "Contract Expiry",
        not_found_msg="_No contract expiry data yet — run `fieldkit watch run contract-expiry` first._",
        dry_run=dry_run,
    )
    draft_queue_configured = get_mcp_endpoint("draft_queue") is not None
    draft_queue_result = _collect_alert_source(
        _draft_queue_alerts_file(),
        target_date,
        "Draft Queue",
        not_found_msg=(
            "_No draft queue data yet — run `fieldkit watch run draft-queue` first._"
            if draft_queue_configured
            else "_Draft-queue input was not run because its endpoint is not configured._"
        ),
        dry_run=dry_run,
    )
    meetings_result = _collect_calendar_meetings(
        target_date,
        internal_domains,
        user_email,
        enabled=not dry_run and calendar_enabled is not False,
    )
    configured_no_llm = llm_disabled() or get_llm_model() is None if no_llm is None else no_llm
    use_no_llm = dry_run or configured_no_llm
    llm_failure: LLMError | None = None
    fallback_review: list[str] = []
    try:
        pipeline_review_result = _collect_pipeline_review(
            no_llm=use_no_llm,
            account=account,
            fallback_review=fallback_review,
        )
    except LLMError as exc:
        llm_failure = exc
        pipeline_review_result = (
            "> [DEGRADED] LLM synthesis failed — this brief was generated without AI synthesis. "
            "Check model configuration and credentials before retrying.\n\n"
            + (fallback_review[0] if fallback_review else "_Pipeline review unavailable after provider failure._")
        )
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
        "pipeline review": pipeline_review_result if pipeline_review_failed or llm_failure is not None else [],
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
        log.info("Dry run — brief not written to disk (duration: %.1fs)", elapsed)
        return MergedBriefResult(
            run=WatcherRunResult("partial" if degraded else "ok", True, None),
            account=account,
            target_date=target_date,
            text=brief_md,
            degraded_sources=degraded,
            dry_run=True,
        )

    output_dir = get_fieldkit_home() / "briefs"
    write_result = _write_brief_to_disk(
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
            pipeline_review_result if pipeline_review_failed or llm_failure is not None else [],
        ],
        dry_run=dry_run,
        output_dir=output_dir,
    )
    return MergedBriefResult(
        run=write_result.run,
        account=account,
        target_date=target_date,
        text=brief_md,
        degraded_sources=degraded,
        path=output_dir / f"morning-brief-{target_date.isoformat()}.md",
        write_result=write_result,
        provider_failure=llm_failure,
    )
