"""CLI and aggregate-watcher adapter for the merged morning brief."""

import json

import click

from fieldkit.brief.merged import generate_merged_brief
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.status import WatcherRunResult


def _run_generate(
    *,
    date_str: str | None,
    dry_run: bool,
    verbose: bool,
    account: str | None = None,
    as_json: bool = False,
    no_llm: bool | None = None,
    calendar_enabled: bool | None = None,
) -> WatcherRunResult:
    """Run with watcher logging and return this invocation's execution facts."""
    with watcher_logging("morning-brief", enabled=not dry_run):
        return _run_generate_inner(
            date_str=date_str,
            dry_run=dry_run,
            verbose=verbose,
            account=account,
            as_json=as_json,
            no_llm=no_llm,
            calendar_enabled=calendar_enabled,
        )


def _run_generate_inner(
    *,
    date_str: str | None,
    dry_run: bool,
    verbose: bool,
    account: str | None = None,
    as_json: bool = False,
    no_llm: bool | None = None,
    calendar_enabled: bool | None = None,
) -> WatcherRunResult:
    """Render the domain result to CLI output and preserve provider failures."""
    result = generate_merged_brief(
        date_str=date_str,
        dry_run=dry_run,
        account=account,
        no_llm=no_llm,
        calendar_enabled=calendar_enabled,
    )
    if result.target_date is None:
        return result.run

    if result.dry_run:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "account": result.account,
                        "date": result.target_date.isoformat(),
                        "degraded_sources": result.degraded_sources,
                        "dry_run": True,
                        "path": None,
                        "written": False,
                    },
                    sort_keys=True,
                )
            )
        else:
            click.echo(result.text)
    elif as_json:
        assert result.write_result is not None
        click.echo(
            json.dumps(
                {
                    "account": result.account,
                    "date": result.target_date.isoformat(),
                    "degraded_sources": result.degraded_sources,
                    "dry_run": False,
                    "path": str(result.path),
                    "written": result.write_result.written,
                    "records_checked": result.write_result.records_checked,
                    "alerts_generated": result.write_result.alerts_generated,
                    "failures": result.write_result.failures,
                },
                sort_keys=True,
            )
        )
    if result.provider_failure is not None:
        raise result.provider_failure
    return result.run
