"""fieldkit companion — attention feed + permission gate for a sidecar agent.

Usage:
    fieldkit companion feed [--json|--markdown] [--all] [--account SLUG]
    fieldkit companion allowed -- <command…>
    fieldkit companion run -- <command…>

The agent brain stays external (design D1): any runtime polls the feed,
checks the gate, and executes through `companion run` — the choke point
that journals every outcome to companion-journal-YYYY-MM.jsonl.
"""

import datetime as dt
import json
from typing import TYPE_CHECKING, cast

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.companion.gate import TIER_ORDER, Tier

if TYPE_CHECKING:
    from fieldkit.companion.decide import ProposedAction
    from fieldkit.companion.feed import AttentionItem
    from fieldkit.companion.llm_decide import DecisionResult

# Exit code for gate denial: EXIT_DATA — denial is permanent, not retriable.
_EXIT_DENIED = 3

# Tier ordering for the loop's lowering-only --tier override, derived from the
# single tier-vocabulary home (companion.gate.TIER_ORDER) so it cannot fork.
_TIER_RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(TIER_ORDER)}


def _validate_act_allowlist(tier: str, allowlist: list[str]) -> None:
    """Reject invalid configured actions before a companion execution path starts."""
    if tier != "act" or not allowlist:
        return

    from fieldkit.companion.gate import validate_allowlist
    from fieldkit.companion.mapping import DRY_RUN_CAPABLE

    problems = validate_allowlist(allowlist, set(DRY_RUN_CAPABLE))
    if problems:
        for problem in problems:
            click.echo(f"act_allowlist config error: {problem}", err=True)
        raise SystemExit(EXIT_DATA)


def _llm_decision(item: "AttentionItem", baseline: "ProposedAction", enrichment: str | None) -> "DecisionResult":
    """Bind the companion domain decision boundary to the shared LLM adapter."""
    from fieldkit.companion.llm_decide import decide_with_llm
    from fieldkit.llm import synthesize, wrap_user_data

    return decide_with_llm(item, baseline, enrichment, synthesize_fn=synthesize, wrap_fn=wrap_user_data)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Agent companion loop — feed, allowed, run."""


@cli.command("feed")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON lines (one item per line).")
@click.option("--markdown", "as_markdown", is_flag=True, default=False, help="Render as markdown for human reading.")
@click.option("--all", "show_all", is_flag=True, default=False, help="Ignore the cursor; show every current item.")
@click.option("--account", "account_slug", default=None, help="Filter to a single account slug.")
def feed(as_json: bool, as_markdown: bool, show_all: bool, account_slug: str | None) -> None:
    """Emit new attention items from watchers, alert files, and TASKS.md.

    Default output is JSON lines. Repeated polls return only items not
    yet delivered (cursor in <fieldkit_data>/companion-cursor.json);
    --all bypasses the cursor without advancing it. Items on a cooldown
    or awaiting review in the outbox are withheld until the cooldown
    expires or the proposal file is deleted.

    Exit codes: 0 — success (with or without items); 2 — config absent;
    3 — malformed upstream state (unparseable status JSON).
    """
    with cli_main():
        from fieldkit.companion.feed import get_feed
        from fieldkit.companion.suppress import retired_item_ids
        from fieldkit.config import get_fieldkit_data, get_fieldkit_home

        home = get_fieldkit_home()
        data_path = get_fieldkit_data()
        items = get_feed(
            home,
            data_path,
            since_cursor=not show_all,
            account_slug=account_slug,
            suppressed=retired_item_ids(data_path),
        )

        if as_markdown:
            if not items:
                click.echo("No new attention items.")
                return
            click.echo("# Attention feed\n")
            for item in items:
                account = f" `{item.account}`" if item.account else ""
                skill = f" → `{item.suggested_skill}`" if item.suggested_skill else ""
                click.echo(f"- **{item.severity.upper()}**{account} {item.summary}{skill}")
                click.echo(f"  evidence: {item.evidence_path}")
            return

        # JSON lines is the default contract (as_json flag kept for explicitness).
        for item in items:
            click.echo(json.dumps(item.to_dict()))


def _retention_days(value: str) -> dt.timedelta:
    if not value.endswith("d") or not value[:-1].isdigit() or int(value[:-1]) <= 0:
        raise click.BadParameter("must be a positive whole-day duration such as 14d", param_hint="--older-than")
    return dt.timedelta(days=int(value[:-1]))


@declare_write("workspace")
@cli.command("prune")
@click.option("--older-than", default="14d", show_default=True, help="Select proposals at least this old (Nd).")
@click.option(
    "--limit", type=click.IntRange(1, 100), default=25, show_default=True, help="Maximum proposals to retire."
)
@click.option("--confirm", is_flag=True, default=False, help="Retire the selected proposals; otherwise preview only.")
@click.option("--dry-run", is_flag=True, default=False, help="Explicitly preview without retiring proposals.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one machine-readable result object.")
def prune(older_than: str, limit: int, confirm: bool, dry_run: bool, as_json: bool) -> None:
    """Preview or retire old pending proposals in a bounded batch.

    Valid version-1 proposals are selected oldest first. Legacy, malformed,
    and execution-marker proposals are preserved. Confirmed expiry keeps the
    condition suppressed for 24 hours and records lifecycle evidence.

    Exit codes: 0 — success; 2 — config absent; 3 — invalid arguments.
    """
    with cli_main():
        from fieldkit.companion.lifecycle import prune_proposals
        from fieldkit.config import get_fieldkit_data

        if confirm and dry_run:
            raise click.BadParameter("cannot be combined with --confirm", param_hint="--dry-run")
        result = prune_proposals(
            get_fieldkit_data(),
            older_than=_retention_days(older_than),
            limit=limit,
            confirm=confirm and not dry_run,
        )
        if as_json:
            click.echo(json.dumps(result.to_dict()))
            return
        mode = "retired" if result.confirmed else "would retire"
        for name in result.retired if result.confirmed else result.selected:
            click.echo(name)
        rate = "unavailable" if result.ignored_rate is None else f"{result.ignored_rate:.1%}"
        click.echo(
            f"{mode} {len(result.retired) if result.confirmed else len(result.selected)} of {result.eligible} eligible "
            f"proposal(s); remaining={result.remaining_eligible}; skipped={len(result.skipped)}; ignored_rate={rate}"
        )


@declare_write("workspace")
@cli.command("reconcile-invalid")
@click.option(
    "--limit", type=click.IntRange(1, 100), default=25, show_default=True, help="Maximum proposals to replace."
)
@click.option("--confirm", is_flag=True, default=False, help="Atomically replace the selected proposals.")
@click.option("--dry-run", is_flag=True, default=False, help="Explicitly preview without replacing proposals.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one machine-readable result object.")
def reconcile_invalid(limit: int, confirm: bool, dry_run: bool, as_json: bool) -> None:
    """Replace fresh invalid-command proposals with deterministic baselines."""
    with cli_main():
        from fieldkit.companion.feed import get_feed
        from fieldkit.companion.lifecycle import reconcile_invalid_command_proposals
        from fieldkit.config import get_fieldkit_data, get_fieldkit_home

        if confirm and dry_run:
            raise click.BadParameter("cannot be combined with --confirm", param_hint="--dry-run")
        data_path = get_fieldkit_data()
        items = get_feed(get_fieldkit_home(), data_path, since_cursor=False, suppressed=set())
        result = reconcile_invalid_command_proposals(data_path, items, limit=limit, confirm=confirm and not dry_run)
        if as_json:
            click.echo(json.dumps(result.to_dict()))
            return
        mode = "reconciled" if result.confirmed else "would reconcile"
        for name in result.reconciled if result.confirmed else result.selected:
            click.echo(name)
        click.echo(
            f"{mode} {len(result.reconciled) if result.confirmed else len(result.selected)} proposal(s); skipped={len(result.skipped)}"
        )


@cli.command("allowed", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def allowed(command: tuple[str, ...], as_json: bool) -> None:
    """Answer whether COMMAND is permitted at the configured tier.

    Exit 0 = permitted; exit 3 = denied (EXIT_DATA — denial is permanent,
    so exit 1 'retry may help' would mislead orchestrators). A malformed
    act_allowlist entry is also a denial: it is checked here before the
    per-invocation question, so a typo'd or non-previewable allowlist entry
    is caught the first time anyone asks what's permitted, not silently at
    execution time.

    --json goes before the ``--`` separator; anything after ``--`` belongs to
    the command being gate-checked, including its own --json.

    Example: fieldkit companion allowed -- pursuit advance acme/deal --dry-run
    """
    with cli_main():
        from fieldkit.companion.gate import is_allowed, validate_allowlist
        from fieldkit.companion.mapping import DRY_RUN_CAPABLE
        from fieldkit.config import get_companion_act_allowlist, get_companion_tier

        allowlist = get_companion_act_allowlist()
        argv = list(command)
        tier = get_companion_tier()

        problems = validate_allowlist(allowlist, set(DRY_RUN_CAPABLE))
        if problems:
            if as_json:
                click.echo(
                    json.dumps(
                        {"allowed": False, "tier": tier, "command": argv, "error": problems},
                        indent=2,
                        default=str,
                    )
                )
            else:
                for problem in problems:
                    click.echo(f"act_allowlist config error: {problem}", err=True)
            raise SystemExit(_EXIT_DENIED)

        permitted = is_allowed(argv, tier, allowlist)

        if as_json:
            # Denial is an answer, not a failure, so the document is emitted on
            # both paths; the exit code still distinguishes them.
            click.echo(json.dumps({"allowed": permitted, "tier": tier, "command": argv}, indent=2, default=str))
        elif permitted:
            click.echo(f"allowed (tier: {tier})")
        else:
            click.echo(f"denied (tier: {tier}): {' '.join(argv)}", err=True)

        if not permitted:
            raise SystemExit(_EXIT_DENIED)


@cli.command("run", context_settings={"ignore_unknown_options": True})
@click.option("--item-id", default="", help="Attention item this action addresses (journaled).")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(item_id: str, command: tuple[str, ...]) -> None:
    """Gate-check COMMAND, execute it, and journal the outcome — one call.

    Exits with the subprocess exit code, or 3 on gate denial (the denial
    is journaled too, so refusals appear in the audit trail).

    Example: fieldkit companion run --item-id abc123 -- pursuit health --json
    """
    with cli_main():
        from fieldkit.companion.runner import run_action
        from fieldkit.config import get_companion_act_allowlist, get_companion_tier, get_fieldkit_data

        argv = list(command)
        if not argv:
            click.echo("no command given — usage: fieldkit companion run -- <command…>", err=True)
            raise SystemExit(EXIT_DATA)

        tier = get_companion_tier()
        allowlist = get_companion_act_allowlist()
        _validate_act_allowlist(tier, allowlist)
        result = run_action(
            argv,
            tier=tier,
            allowlist=allowlist,
            data_path=get_fieldkit_data(),
            item_id=item_id,
        )
        if result.denied:
            click.echo(f"denied: {' '.join(argv)}", err=True)
            raise SystemExit(_EXIT_DENIED)
        if result.stdout:
            click.echo(result.stdout, nl=False)
        if result.stderr:
            click.echo(result.stderr, nl=False, err=True)
        if result.exit_code != 0:
            raise SystemExit(result.exit_code)


@declare_write("workspace")
@cli.command("loop")
@click.option("--once", is_flag=True, default=False, help="Run a single pass (the only supported mode).")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Compute proposals without writing, running, or journaling.",
)
@click.option(
    "--tier",
    "tier_override",
    default=None,
    help="Lower the effective tier for this run (may not exceed the configured tier).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the LoopResult as JSON.")
def loop(once: bool, dry_run: bool, tier_override: str | None, as_json: bool) -> None:
    """Run one poll→decide→gate→act→journal pass over the attention feed.

    The headless companion brain: polls the feed, gathers deterministic context,
    and may refine proposals through the configured Vertex-routed LLM. Honors
    ``companion.tier`` (read/propose/act); ``--tier`` can only LOWER it. At
    propose tier, writes one companion-outbox proposal per item without running
    its candidate command. At act tier, the candidate still must pass the exact
    configured allowlist before execution. Read tier and ``NO_LLM=1`` retain
    deterministic behavior.

    Exit codes: 0 — success; 1 — partial (a best-effort context read failed);
    3 — malformed feed input or an invalid/escalating --tier.
    """
    _ = once  # single pass is the only mode; the flag reserves space for future cadences.
    with cli_main():
        from fieldkit.companion.loop import run_once
        from fieldkit.config import (
            get_companion_act_allowlist,
            get_companion_tier,
            get_fieldkit_data,
            get_fieldkit_home,
        )

        configured = get_companion_tier()
        effective = configured
        if tier_override is not None:
            requested = tier_override.strip().lower()
            if requested not in _TIER_RANK:
                click.echo(f"unknown --tier {requested!r} (expected read|propose|act)", err=True)
                raise SystemExit(EXIT_DATA)
            if _TIER_RANK[requested] > _TIER_RANK[configured]:
                click.echo(f"--tier {requested} exceeds configured tier {configured}; refusing to escalate", err=True)
                raise SystemExit(EXIT_DATA)
            effective = requested

        if effective not in TIER_ORDER:  # defensive; get_companion_tier fails closed to a valid tier
            effective = "read"
        allowlist = get_companion_act_allowlist()
        _validate_act_allowlist(effective, allowlist)
        result = run_once(
            get_fieldkit_home(),
            get_fieldkit_data(),
            tier=cast(Tier, effective),
            allowlist=allowlist,
            dry_run=dry_run,
            decision_fn=_llm_decision,
        )

        if as_json:
            click.echo(json.dumps(result.to_dict()))
        else:
            prefix = "[dry-run] " if dry_run else ""
            click.echo(
                f"{prefix}tier={result.tier} handled={result.handled} proposed={result.proposed} "
                f"triaged={result.triaged} unresolved={result.unresolved} enriched={result.enriched} acted={result.acted} "
                f"denied={result.denied} enrich_failures={result.enrich_failures} "
                f"auth_failures={result.auth_failures} dropped={result.dropped} "
                f"llm_attempts={result.llm_attempts} llm_decisions={result.llm_decisions} "
                f"llm_fallbacks={result.llm_fallbacks}"
            )
        if result.auth_failures:
            raise SystemExit(EXIT_AUTH)
        if result.partial:
            raise SystemExit(EXIT_PARTIAL)
