"""CLI adapter for bounded Gmail-derived CRM-review candidates."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import click

from fieldkit.config import get_accounts_config
from fieldkit.errors import GmailSyncPartialError
from fieldkit.gmail import backstory_gap as domain
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.query_domain import connect
from fieldkit.gmail.query_support import DEFAULT_QUERY_LIMIT, MAX_QUERY_LIMIT, markdown_cell


def _render_account(account: str, contacts: list[domain.ContactCandidate], min_messages: int, as_of: str) -> str:
    lines = [
        f"## {markdown_cell(account)}",
        "",
        f"*As of {as_of} — contacts with ≥{min_messages} messages; CRM comparison was not performed*",
        "",
    ]
    if not contacts:
        return "\n".join([*lines, "_No CRM-review candidates found at this threshold._", ""])
    lines.extend(
        [
            f"**{len(contacts)} contact candidate(s) visible in the published Gmail cache**",
            "",
            "| Email | Name | Msgs | Threads | Meetings | Slack | Last Seen |",
            "|-------|------|------|---------|----------|-------|-----------|",
        ]
    )
    for contact in contacts:
        lines.append(
            f"| {markdown_cell(contact.email)} | {markdown_cell(contact.name)} "
            f"| {contact.message_count} | {contact.thread_count} | {contact.meeting_count} "
            f"| {contact.slack_message_count} | {contact.last_seen or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _emit_json(
    report: domain.CandidateReport,
    *,
    as_of: str,
    account: str | None,
    min_messages: int | None,
    limit: int,
) -> None:
    click.echo(
        json.dumps(
            {
                "items": [asdict(item) for item in report.items],
                "count": len(report.items),
                "truncated": report.truncated,
                "scan_truncated": report.scan_truncated,
                "scanned_rows": report.scanned_rows,
                "as_of": as_of,
                "comparison": {"crm": "not_performed", "result_kind": "review_candidates"},
                "filters": {"account": account, "min_messages": min_messages, "limit": limit},
            },
            indent=2,
        )
    )


@click.command(name="backstory-gap")
@click.option("--account", default=None, metavar="SLUG", help="Restrict to one configured account key.")
@click.option(
    "--min-messages",
    type=click.IntRange(min=0),
    default=None,
    help="Minimum message count. Defaults to the configured per-account threshold.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=MAX_QUERY_LIMIT),
    default=DEFAULT_QUERY_LIMIT,
    show_default=True,
    help="Maximum candidates across all selected accounts.",
)
@click.option(
    "--db", default=lambda: str(get_gmail_db_path()), help="Path to the managed Gmail cache.", show_default=True
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def cli(account: str | None, min_messages: int | None, limit: int, db: str, as_json: bool) -> None:
    """Find Gmail contacts that require a separate CRM comparison."""
    scopes = domain.account_scopes(
        get_accounts_config(strict=True),
        account=account,
        min_messages=min_messages,
    )
    with connect(Path(db)) as connection:
        report = domain.query_candidates(connection, scopes, limit=limit)
    as_of = datetime.now(UTC).strftime("%Y-%m-%d")
    if as_json:
        _emit_json(report, as_of=as_of, account=account, min_messages=min_messages, limit=limit)
        if report.scan_truncated:
            raise GmailSyncPartialError("Gmail candidate scan reached its bounded work budget")
        return

    click.echo("# CRM Review Candidate Report")
    click.echo("")
    click.echo("Contacts visible in the ready published Gmail cache. CRM comparison was not performed.")
    click.echo(f"Generated: {as_of}")
    click.echo("")
    grouped: dict[str, list[domain.ContactCandidate]] = {scope.key: [] for scope in scopes}
    for item in report.items:
        grouped[item.account].append(item)
    for scope in scopes:
        click.echo(_render_account(scope.key, grouped[scope.key], scope.min_messages, as_of))
    click.echo("---")
    click.echo(f"**Total CRM-review candidates: {len(report.items)}**")
    if report.truncated:
        click.echo(f"**Results truncated at --limit {limit}.**")
    if report.scan_truncated:
        click.echo("**Candidate scan reached its bounded work budget; results are incomplete.**")
        raise GmailSyncPartialError("Gmail candidate scan reached its bounded work budget")
