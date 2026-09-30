"""fieldkit issue — GitHub Issues-backed tracker for bugs and enhancement requests.

Usage:
    fieldkit issue create    --type bug --title "..." [options]
    fieldkit issue list      [--status open|closed|wont-fix|all] [--type bug|enhancement|all]
    fieldkit issue show      <id>
    fieldkit issue close     <id> [--wont-fix]
    fieldkit issue reopen    <id>
    fieldkit issue edit      <id> [--title ...] [--body ...] [--severity ...] [--module ...]
    fieldkit issue fix       <id> [--commit SHA] [--note TEXT]
    fieldkit issue note      <id> <text>
    fieldkit issue plan      <id>
    fieldkit issue link      <id> <milestone>
    fieldkit issue sync-milestone <milestone> --state queued|completed
    fieldkit issue board     (kanban summary in terminal)
"""

import dataclasses
import json
from collections.abc import Sequence

import click

from fieldkit.cli_exit import EXIT_DATA
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_github_repo
from fieldkit.issue import (
    ISSUE_MODULES,
    ISSUE_SEVERITIES,
    ISSUE_TYPES,
    SEVERITY_RANK,
    AdvanceOutcome,
    GHIssue,
    GHIssueStore,
    IssueSeverity,
    IssueStatus,
    IssueType,
    MilestonePlan,
    MilestoneState,
    advance_milestone_candidates,
    milestone_exit_code,
    plan_milestone_sync,
)
from fieldkit.util.jsonio import json_default

# Every mutating `issue` subcommand POSTs/PATCHes the GitHub REST API via
# GHIssueStore, so each is an external write. None of them take --confirm: the
# driver loop and the `raise-issue` skill invoke them unattended, where an
# interactive prompt would hang the run rather than protect anything. That
# trade-off is recorded per-command as an explicit exemption instead of being
# hidden behind a "read-only" classification inferred from the absent flag.
_UNATTENDED = "invoked unattended by the driver loop and raise-issue skill; a prompt would deadlock the run"


def _store() -> GHIssueStore:
    return GHIssueStore(get_github_repo())


def _severity_color(severity: str) -> str:
    return {
        "critical": "bright_red",
        "high": "red",
        "medium": "yellow",
        "low": "green",
    }.get(severity, "white")


def _status_color(status: str) -> str:
    return {
        "open": "cyan",
        "planned": "blue",
        "fixed": "green",
        "closed": "bright_black",
        "wont-fix": "bright_black",
    }.get(status, "white")


@click.group(
    name="issue",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """GitHub Issues tracker — create bugs and enhancement requests.

    fieldkit-created issues use the public ``fieldkit-<number>`` identifier;
    their ``bug`` or ``enhancement`` label records the issue type.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("create")
@click.option("--type", "issue_type", type=click.Choice(ISSUE_TYPES), required=True, help="Issue type.")
@click.option("--title", required=True, help="Short title (one sentence).")
@click.option("--body", default="", help="Detailed description. Markdown supported.")
@click.option(
    "--severity",
    type=click.Choice(ISSUE_SEVERITIES),
    default="medium",
    show_default=True,
    help="Severity level (bugs) or impact (enhancements).",
)
@click.option("--module", type=click.Choice(ISSUE_MODULES), default="other", show_default=True)
@click.option("--source", default="unknown", show_default=True, help="Who raised this (agent name, user, etc.).")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def create_cmd(
    issue_type: IssueType,
    title: str,
    body: str,
    severity: IssueSeverity,
    module: str,
    source: str,
    as_json: bool,
) -> None:
    """Create a new bug or enhancement request."""
    store = _store()
    issue = store.create(
        issue_type=issue_type,
        title=title,
        body=body,
        severity=severity,
        module=module,
        source=source,
    )
    repo = get_github_repo()
    url = f"https://github.com/{repo}/issues/{issue.gh_number}"
    if as_json:
        click.echo(json.dumps({**dataclasses.asdict(issue), "url": url}, indent=2, default=json_default))
        return
    click.echo(f"[issue] Created {click.style(issue.id, bold=True, fg='cyan')} — {issue.title}")
    click.echo(f"[issue] GitHub: {url}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@cli.command("list")
@click.option(
    "--status",
    type=click.Choice(["open", "planned", "fixed", "closed", "wont-fix", "all"]),
    default="open",
    show_default=True,
    help="Filter by status. Default: open.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all issues regardless of status (shorthand for --status all).",
)
@click.option(
    "--type",
    "issue_type",
    type=click.Choice(["bug", "enhancement", "all"]),
    default="all",
    show_default=True,
)
@click.option("--module", default=None, help="Filter by module.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def list_cmd(status: str, show_all: bool, issue_type: str, module: str | None, as_json: bool) -> None:
    """List issues. Default shows only open issues; use --all to see everything."""
    if show_all:
        status = "all"
    store = _store()
    issues = store.list_issues(status=status, issue_type=issue_type, module=module)  # type: ignore[arg-type]
    filters = {"status": status, "type": issue_type, "module": module}

    if not issues:
        if as_json:
            click.echo(json.dumps({"items": [], "count": 0, "filters": filters}, indent=2, default=json_default))
            return
        label = "" if status == "all" else f"{status} "
        click.echo(f"No {label}issues found.")
        return

    issues.sort(key=lambda i: (SEVERITY_RANK[i.severity], i.created))

    if as_json:
        payload = {
            "items": [dataclasses.asdict(i) for i in issues],
            "count": len(issues),
            "filters": filters,
        }
        click.echo(json.dumps(payload, indent=2, default=json_default))
        return

    click.echo(f"\n{'ID':<10} {'TYPE':<14} {'SEV':<10} {'MODULE':<12} {'#':<6} TITLE")
    click.echo("-" * 90)
    for issue in issues:
        sev_str = click.style(f"{issue.severity:<10}", fg=_severity_color(issue.severity))
        type_str = f"{'[' + issue.type + ']':<14}"
        click.echo(
            f"{click.style(issue.id, bold=True):<10} {type_str} {sev_str} "
            f"{issue.module:<12} {f'#{issue.gh_number}':<6} {issue.title}"
        )
    click.echo(f"\n{len(issues)} issue(s) — filter: status={status} type={issue_type}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@cli.command("show")
@click.argument("issue_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def show_cmd(issue_id: str, as_json: bool) -> None:
    """Show details for a specific issue."""
    store = _store()
    issue = store.find(issue_id.lower())
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)

    repo = get_github_repo()
    if as_json:
        url = f"https://github.com/{repo}/issues/{issue.gh_number}"
        click.echo(json.dumps({**dataclasses.asdict(issue), "url": url}, indent=2, default=json_default))
        return

    click.echo(f"\n{'─' * 60}")
    click.echo(f"  {click.style(issue.id, bold=True, fg='cyan')}  {issue.title}")
    click.echo(f"{'─' * 60}")
    click.echo(f"  Type:     {issue.type}")
    click.echo(f"  Status:   {click.style(issue.status, fg=_status_color(issue.status))}")
    click.echo(f"  Severity: {click.style(issue.severity, fg=_severity_color(issue.severity))}")
    click.echo(f"  Module:   {issue.module}")
    click.echo(f"  Source:   {issue.source}")
    click.echo(f"  Created:  {issue.created.strftime('%Y-%m-%d %H:%M UTC')}")
    click.echo(f"  GitHub:   https://github.com/{repo}/issues/{issue.gh_number}")
    click.echo(f"\n  Description\n  {'─' * 40}")
    for line in (issue.body or "_No description._").splitlines():
        click.echo(f"  {line}")
    click.echo()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("close")
@click.argument("issue_id")
@click.option("--wont-fix", "wont_fix", is_flag=True, default=False, help="Mark as won't fix instead of closed.")
@click.option(
    "--skip-verify",
    "skip_verify",
    is_flag=True,
    default=False,
    help="Close even if the issue has not been marked fixed first.",
)
@click.option("--note", default=None, help="Attach a note (e.g. verification results, reason for wont-fix).")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def close_cmd(issue_id: str, wont_fix: bool, skip_verify: bool, note: str | None, as_json: bool) -> None:
    """Close an issue (mark as closed or wont-fix).

    Issues should be marked fixed first (`fieldkit issue fix <ID>`) and only
    closed after operator verification. Use --skip-verify to override.
    """
    store = _store()
    issue = store.find(issue_id.lower())
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)

    if not wont_fix and issue.status != "fixed" and not skip_verify:
        click.echo(
            f"[issue] {issue.id} is {issue.status!r} — use "
            f"{click.style(f'fieldkit issue fix {issue.id}', bold=True)} first, "
            "or pass --skip-verify to override.",
            err=True,
        )
        raise SystemExit(EXIT_DATA)

    new_status: IssueStatus = "wont-fix" if wont_fix else "closed"
    updated = store.update_status(issue_id.lower(), new_status, note=note)
    if updated is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(updated), indent=2, default=json_default))
        return
    click.echo(f"[issue] {updated.id} → {click.style(new_status, fg=_status_color(new_status))}")


# ---------------------------------------------------------------------------
# reopen
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("reopen")
@click.argument("issue_id")
@click.option("--note", default=None, help="Attach a note explaining why this issue is being reopened.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def reopen_cmd(issue_id: str, note: str | None, as_json: bool) -> None:
    """Reopen a closed or wont-fix issue."""
    store = _store()
    existing = store.find(issue_id.lower())
    if existing is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if existing.status == "open":
        # Already-open is a no-op that still exits 0, so --json owes the caller a
        # document rather than an empty stdout it would fail to parse.
        if as_json:
            click.echo(json.dumps(dataclasses.asdict(existing), indent=2, default=json_default))
            return
        click.echo(f"[issue] {existing.id} is already open.", err=True)
        return
    issue = store.update_status(issue_id.lower(), "open", note=note)
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(issue), indent=2, default=json_default))
        return
    click.echo(f"[issue] {issue.id} → {click.style('open', fg=_status_color('open'))}")


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("fix")
@click.argument("issue_id")
@click.option("--commit", default=None, help="Git commit SHA to record with this fix.")
@click.option("--note", default=None, help="Attach a note (e.g. test results) to this transition.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def fix_cmd(issue_id: str, commit: str | None, note: str | None, as_json: bool) -> None:
    """Mark an issue as fixed — code committed, awaiting verification (open|planned → fixed)."""
    store = _store()
    issue = store.find(issue_id.lower())
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if issue.status == "wont-fix":
        click.echo(
            f"[issue] {issue.id} is {issue.status!r} — reopen it before marking it fixed.",
            err=True,
        )
        raise SystemExit(EXIT_DATA)
    updated = store.mark_fixed(issue_id.lower(), commit=commit, note=note)
    if updated is None:
        click.echo(f"[issue] Update failed for {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps({**dataclasses.asdict(updated), "commit": commit}, indent=2, default=json_default))
        return
    msg = f"[issue] {updated.id} → {click.style('fixed', fg=_status_color('fixed'))}"
    if commit:
        msg += f"  (commit {commit})"
    click.echo(msg)
    click.echo(
        f"[issue] Run {click.style(f'fieldkit issue close {updated.id}', bold=True)} after operator verification."
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("plan")
@click.argument("issue_id")
@click.option("--note", default=None, help="Attach a note to this status transition.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def plan_cmd(issue_id: str, note: str | None, as_json: bool) -> None:
    """Mark an issue as planned (open → planned)."""
    store = _store()
    issue = store.find(issue_id.lower())
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if issue.status not in ("open", "planned"):
        click.echo(
            f"[issue] {issue.id} is {issue.status!r} — only open issues can be planned.",
            err=True,
        )
        raise SystemExit(EXIT_DATA)
    updated = store.update_status(issue_id.lower(), "planned", note=note)
    if updated is None:
        click.echo(f"[issue] Update failed for {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(updated), indent=2, default=json_default))
        return
    click.echo(f"[issue] {updated.id} → {click.style('planned', fg=_status_color('planned'))}")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("edit")
@click.argument("issue_id")
@click.option("--title", default=None, help="New title.")
@click.option("--body", default=None, help="Replace body text.")
@click.option("--severity", type=click.Choice(ISSUE_SEVERITIES), default=None)
@click.option("--module", type=click.Choice(ISSUE_MODULES), default=None, help="Affected module.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def edit_cmd(
    issue_id: str,
    title: str | None,
    body: str | None,
    severity: IssueSeverity | None,
    module: str | None,
    as_json: bool,
) -> None:
    """Edit issue metadata (title, body, severity, module)."""
    store = _store()
    issue = store.edit(issue_id.lower(), title=title, body=body, severity=severity, module=module)
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(issue), indent=2, default=json_default))
        return
    click.echo(f"[issue] {issue.id} updated.")


# ---------------------------------------------------------------------------
# note
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("note")
@click.argument("issue_id")
@click.argument("text")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def note_cmd(issue_id: str, text: str, as_json: bool) -> None:
    """Add a note to an issue without changing its status.

    Example: fieldkit issue note <issue-id> "Verified on staging — all good."
    """
    store = _store()
    issue = store.add_note(issue_id.lower(), text)
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        click.echo(json.dumps(dataclasses.asdict(issue), indent=2, default=json_default))
        return
    click.echo(f"[issue] Note added to {issue.id}.")


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("link")
@click.argument("issue_id")
@click.argument("milestone_id")
@click.option("--note", default=None, help="Attach a note to record the association.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def link_cmd(issue_id: str, milestone_id: str, note: str | None, as_json: bool) -> None:
    """Link an issue to a GitHub milestone, for example M001 or 'Sprint 3'.

    Creates the milestone on GitHub if it does not already exist.
    Once linked, `fieldkit issue sync-milestone` can bulk-advance statuses.
    """
    store = _store()
    issue = store.link_milestone(issue_id.lower(), milestone_id.upper(), note=note)
    if issue is None:
        click.echo(f"[issue] Not found: {issue_id}", err=True)
        raise SystemExit(EXIT_DATA)
    if as_json:
        payload = {**dataclasses.asdict(issue), "milestone": milestone_id.upper()}
        click.echo(json.dumps(payload, indent=2, default=json_default))
        return
    click.echo(f"[issue] {issue.id} linked to milestone {click.style(milestone_id.upper(), bold=True)}")


# ---------------------------------------------------------------------------
# sync-milestone
# ---------------------------------------------------------------------------


def _echo_one_outcome(outcome: AdvanceOutcome, target_status: IssueStatus) -> None:
    """Report one issue's result on the human-readable path."""
    updated = outcome.updated
    if updated is not None:
        styled = click.style(target_status, fg=_status_color(target_status))
        click.echo(f"  {updated.id} → {styled}  {updated.title}")
    else:
        click.echo(f"  {outcome.issue_id} update FAILED ({outcome.failure})", err=True)


def _emit_milestone_json(
    *,
    mid: str,
    state: str,
    dry_run: bool,
    plan: MilestonePlan,
    outcomes: list[AdvanceOutcome],
) -> None:
    """Emit the run summary. One builder because this command has several exit
    paths — hand-written dicts per path would drift apart on the next edit."""
    payload = {
        "milestone": mid,
        "state": state,
        "dry_run": dry_run,
        "target_status": plan.target_status,
        "linked": plan.linked,
        "skipped": plan.skipped,
        "candidates": [{"id": issue.id, "status": issue.status} for issue in plan.candidates],
        "advanced": sum(1 for o in outcomes if o.updated is not None),
        "failed": [
            {"id": outcome.issue_id, "category": outcome.failure} for outcome in outcomes if outcome.failure is not None
        ],
    }
    click.echo(json.dumps(payload, indent=2, default=json_default))


def _sync_milestone_prose(
    store: GHIssueStore,
    plan: MilestonePlan,
    *,
    mid: str,
    state: str,
    dry_run: bool,
    commit: str | None,
) -> list[AdvanceOutcome]:
    """Human-readable path: header, per-issue progress, then a summary line."""
    if not plan.linked:
        click.echo(f"[issue] No issues linked to milestone {mid}.")
        return []

    skipped_note = f", {plan.skipped} skipped (already at target or beyond)" if plan.skipped else ""
    click.echo(
        f"[issue] Milestone {mid} ({state}): {len(plan.candidates)} to advance ({plan.action_label})" + skipped_note
    )

    if not plan.candidates:
        click.echo("[issue] Nothing to do.")
        return []

    if dry_run:
        for issue in plan.candidates:
            click.echo(f"  would advance {issue.id}  {issue.status!r} → {plan.target_status!r}  {issue.title}")
        return []

    outcomes = advance_milestone_candidates(store, plan, commit=commit)
    for outcome in outcomes:
        _echo_one_outcome(outcome, plan.target_status)
    advanced = sum(1 for o in outcomes if o.updated is not None)
    click.echo(f"[issue] Advanced {advanced}/{len(plan.candidates)} issue(s).")
    return outcomes


@declare_write("external", confirm_exempt=_UNATTENDED)
@cli.command("sync-milestone")
@click.argument("milestone_id")
@click.option(
    "--state",
    type=click.Choice(["queued", "completed"]),
    required=True,
    help="queued → open issues become planned. completed → planned issues become fixed.",
)
@click.option("--commit", default=None, help="Git commit SHA to record when --state completed.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would change without writing.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def sync_milestone_cmd(
    milestone_id: str,
    state: MilestoneState,
    commit: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Bulk-advance issues linked to a GitHub milestone based on milestone state.

    \b
    queued:    open → planned  (milestone was queued/added to roadmap)
    completed: planned → fixed (milestone was completed)

    Issues already in the target status, or further ahead, are skipped.
    """
    store = _store()
    mid = milestone_id.upper()
    plan = plan_milestone_sync(store.list_by_milestone(mid), state)

    if as_json:
        outcomes = advance_milestone_candidates(store, plan, commit=commit) if plan.candidates and not dry_run else []
        _emit_milestone_json(mid=mid, state=state, dry_run=dry_run, plan=plan, outcomes=outcomes)
    else:
        outcomes = _sync_milestone_prose(store, plan, mid=mid, state=state, dry_run=dry_run, commit=commit)

    # The full bounded outcome document is emitted before the non-zero batch
    # status so callers can reconcile already-applied transitions safely.
    exit_code = milestone_exit_code(outcomes)
    if exit_code:
        raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------


@cli.command("board")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def board_cmd(as_json: bool) -> None:
    """Show a kanban-style terminal board of open issues."""
    store = _store()
    all_open = store.list_issues(status="open")
    all_planned = store.list_issues(status="planned")
    all_fixed = store.list_issues(status="fixed")
    all_closed = store.list_issues(status="closed")
    all_wontfix = store.list_issues(status="wont-fix")

    bugs = [i for i in all_open if i.type == "bug"]
    enhs = [i for i in all_open if i.type == "enhancement"]

    if as_json:
        # Same canonical severity ordering the rendered board uses.
        def _by_severity(issues: Sequence[GHIssue]) -> list[dict[str, object]]:
            ordered = sorted(issues, key=lambda i: SEVERITY_RANK[i.severity])
            return [dataclasses.asdict(i) for i in ordered]

        payload = {
            "counts": {
                "open": len(all_open),
                "planned": len(all_planned),
                "fixed": len(all_fixed),
                "closed": len(all_closed),
                "wont-fix": len(all_wontfix),
            },
            "bugs": _by_severity(bugs),
            "enhancements": _by_severity(enhs),
        }
        click.echo(json.dumps(payload, indent=2, default=json_default))
        return

    def _section(label: str, issues: Sequence[GHIssue], color: str) -> None:
        click.echo(f"\n{click.style(f'  {label} ({len(issues)})', bold=True, fg=color)}")
        click.echo(f"  {'─' * 50}")
        if not issues:
            click.echo("  (none)")
            return
        for issue in sorted(
            issues,
            key=lambda i: SEVERITY_RANK[i.severity],
        ):
            sev = click.style(f"[{issue.severity[:3].upper()}]", fg=_severity_color(issue.severity))
            title_display = (issue.title[:65] + "…") if len(issue.title) > 65 else issue.title
            click.echo(f"  {issue.id:<10} {sev} {issue.module:<10} #{issue.gh_number:<5} {title_display}")

    click.echo(f"\n{'═' * 65}")
    click.echo(
        f"  fieldkit Issue Board — "
        f"{len(all_open)} open | "
        f"{click.style(str(len(all_planned)) + ' planned', fg='blue')} | "
        f"{click.style(str(len(all_fixed)) + ' fixed', fg='green')} | "
        f"{len(all_closed)} closed | "
        f"{len(all_wontfix)} wont-fix"
    )
    click.echo(f"{'═' * 65}")
    _section("Bugs", bugs, "red")
    _section("Enhancements", enhs, "blue")
    click.echo()
