"""fieldkit ingest route — route meetings from accounts/unknown/ to correct account dirs."""

import json
from pathlib import Path
from typing import Literal

import click

from fieldkit.cli_exit import EXIT_DATA
from fieldkit.cli_registry import declare_write
from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.ingest.route_batch import (
    RouteError,
    RouteItem,
    RouteMutationError,
    _force_one_file,
    _route_item,
    _route_one_file,
)
from fieldkit.pursuit.io import split_frontmatter_raw, write_frontmatter_raw


@declare_write("workspace")
@click.command("route")
@click.option("--dry-run", is_flag=True, help="Report what would be moved without making changes.")
@click.option(
    "--data-root",
    "data_root_override",
    default=None,
    type=click.Path(path_type=Path),
    help="Override data root (default: from config).",
)
@click.option(
    "--file",
    "target_file",
    default=None,
    metavar="NAME",
    help="Single file in accounts/unknown/meetings/ to act on. Required with --force-account.",
)
@click.option(
    "--force-account",
    "force_account",
    default=None,
    metavar="SLUG",
    help="Assign --file to this account, skipping the matchers. Requires --file.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the routing summary as JSON.")
def cli(
    dry_run: bool,
    data_root_override: Path | None,
    target_file: str | None,
    force_account: str | None,
    as_json: bool,
) -> None:
    """Route meetings from accounts/unknown/ to correct account directories.

    Uses filename-prefix matching first (e.g. acme-corp-2026-05-01-*.md),
    then falls back to title-keyword matching from accounts.yaml.

    Files with ``reviewed-unmatched: true`` or ``ambiguous-match`` in their
    frontmatter are skipped (idempotency guard).

    \b
    Results written to frontmatter:
      - Single match: moves file, updates ``account`` field
      - Multiple matches: writes ``ambiguous-match: [acct1, acct2]``
      - No match: writes ``reviewed-unmatched: true``

    When the matchers cannot decide, resolve the leftover by hand:

    \b
        fieldkit ingest route --file 2026-06-18-quarterly.md --force-account acme-corp

    A force applies to one named file. It deliberately overrides the guards
    above, since those mark precisely the files that need manual resolution.

    Exit codes: 0 success; 3 unknown account slug, missing file, or
    --file/--force-account used without the other; 1 partial when a batch
    filesystem mutation fails after processing began.
    """
    from fieldkit.config import get_accounts_config, get_fieldkit_home

    if (target_file is None) != (force_account is None):
        click.echo("Error: --file and --force-account must be used together.", err=True)
        raise SystemExit(EXIT_DATA)

    validate_account_slug(force_account)

    data_root = data_root_override if data_root_override is not None else get_fieldkit_home()
    unknown_meetings = data_root / "accounts" / "unknown" / "meetings"

    filters = {"file": target_file, "force_account": force_account, "dry_run": dry_run}

    def _emit(
        counts: dict[str, int],
        considered: int,
        mode: str,
        *,
        items: list[RouteItem] | None = None,
        outcome: Literal["ok", "partial", "error"] = "ok",
        aborted_at: str | None = None,
        error: RouteError | None = None,
        note: str | None = None,
    ) -> None:
        emitted_items = items or []
        click.echo(
            json.dumps(
                {
                    "mode": mode,
                    "considered": considered,
                    "items": emitted_items,
                    "count": len(emitted_items),
                    "moved": counts.get("moved", 0),
                    "ambiguous": counts.get("ambiguous", 0),
                    "unmatched": counts.get("unmatched", 0),
                    "skipped": counts.get("skipped", 0),
                    "note": note,
                    "filters": filters,
                    "outcome": outcome,
                    "aborted_at": aborted_at,
                    "error": error,
                },
                indent=2,
                default=str,
            )
        )

    if not unknown_meetings.exists():
        if as_json:
            _emit({}, considered=0, mode="scan", note="No accounts/unknown/meetings/ directory found.")
            return
        click.echo("No accounts/unknown/meetings/ directory found.")
        return

    if force_account is not None and target_file is not None:
        # Accept a bare name or a path; resolve inside unknown/meetings either way
        # so a force cannot reach outside it.
        candidate = unknown_meetings / Path(target_file).name
        if not candidate.is_file():
            click.echo(f"Error: {Path(target_file).name} not found in accounts/unknown/meetings/.", err=True)
            raise SystemExit(EXIT_DATA)
        force_counters: dict[str, int] = {"moved": 0}
        rc = _force_one_file(
            candidate,
            force_account,
            data_root,
            dry_run=dry_run,
            counters=force_counters,
            split_frontmatter=split_frontmatter_raw,
            write_frontmatter=write_frontmatter_raw,
            quiet=as_json,
        )
        if as_json:
            # The force ran — emit the outcome even when it failed (rc 3).
            force_item = _route_item(
                candidate,
                "would-move" if dry_run else ("moved" if rc == 0 else "error"),
                ok=rc == 0,
                frontmatter_updated=rc == 0 and not dry_run,
                move_completed=rc == 0 and not dry_run,
            )
            force_error: RouteError | None = None
            if rc != 0:
                force_error = {"code": "route-data-error", "operation": "load-frontmatter"}
            _emit(
                force_counters,
                considered=1,
                mode="force",
                items=[force_item],
                outcome="ok" if rc == 0 else "error",
                aborted_at=candidate.name if rc != 0 else None,
                error=force_error,
            )
            raise SystemExit(rc)
        label = "[DRY-RUN] " if dry_run else ""
        if rc == 0:
            click.echo(f"\n{label}Summary: 1 file forced to {force_account}.")
        raise SystemExit(rc)

    files = [f for f in unknown_meetings.glob("*.md") if not f.name.startswith(".") and f.name != ".gitkeep"]
    if not files:
        if as_json:
            _emit({}, considered=0, mode="scan", note="No meeting files in accounts/unknown/meetings/.")
            return
        click.echo("No meeting files in accounts/unknown/meetings/.")
        return

    accounts_cfg_raw = get_accounts_config()
    account_names: list[str] = list(
        (accounts_cfg_raw.get("accounts", {}) if isinstance(accounts_cfg_raw, dict) else {}).keys()
    )

    counters: dict[str, int] = {"moved": 0, "ambiguous": 0, "unmatched": 0, "skipped": 0}

    items: list[RouteItem] = []
    route_error: RouteError | None = None
    aborted_at: str | None = None
    for fpath in sorted(files):
        try:
            item = _route_one_file(
                fpath,
                account_names,
                data_root,
                dry_run=dry_run,
                counters=counters,
                split_frontmatter=split_frontmatter_raw,
                write_frontmatter=write_frontmatter_raw,
                quiet=as_json,
            )
        except RouteMutationError as exc:
            item = _route_item(
                fpath,
                "error",
                ok=False,
                frontmatter_updated=exc.frontmatter_updated,
            )
            route_error = {"code": "route-mutation-failed", "operation": exc.operation}
            aborted_at = exc.item
            if not as_json:
                click.echo(f"  ERROR: {exc.operation} failed for {exc.item}; routing stopped.", err=True)
            items.append(item)
            break
        items.append(item)

    if as_json:
        _emit(
            counters,
            considered=len(files),
            mode="scan",
            items=items,
            outcome="partial" if route_error else "ok",
            aborted_at=aborted_at,
            error=route_error,
        )
        if route_error:
            raise SystemExit(1)
        return

    label = "[DRY-RUN] " if dry_run else ""
    status = "PARTIAL " if route_error else ""
    attempt_detail = f" {len(items)} of {len(files)} attempted, 1 error." if route_error else ""
    click.echo(
        f"\n{label}{status}Summary: {counters['moved']} moved, {counters['ambiguous']} ambiguous, "
        f"{counters['unmatched']} unmatched, {counters['skipped']} skipped.{attempt_detail}"
    )
    if route_error:
        raise SystemExit(1)
