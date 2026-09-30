"""sf_pipeline listview — Salesforce list view sync pipeline.

Extracts opportunity data from Salesforce via SOSL search,
matches to local pursuit files, and writes cache + frontmatter.

Usage:
    fieldkit sf listview [account_name|--all]

account_name: a configured account slug such as acme-corp
--all: sync all accounts (default)
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.commands.sf.listview_render import print_sync_summary, print_untracked_table
from fieldkit.config import get_accounts_config, get_fieldkit_home, get_sf_rest_base_url, get_sf_session_id
from fieldkit.errors import AuthError, FieldkitError
from fieldkit.sf import errors as sf_errors
from fieldkit.sf.errors import reauth_hint_message as _reauth_hint
from fieldkit.sf.sync import configured_accounts, match_pursuit, sync_opportunity

LOG_PREFIX = "[sf-listview-sync]"

# implementation change: module-level quiet flag set by cli() so all _log() calls in helper
# functions are automatically suppressed without threading through the parameter.
_quiet_mode: bool = False


def _log(msg: str) -> None:
    if not _quiet_mode:
        click.echo(f"{LOG_PREFIX} {msg}", err=True)


def _error(msg: str) -> None:
    """Emit an actionable diagnostic even when progress output is quiet."""
    click.echo(f"{LOG_PREFIX} ERROR: {msg}", err=True)


def _process_opp(
    opp: dict[str, str],
    pursuit_dir: str,
    *,
    write: bool = True,
) -> tuple[int, int, int, dict[str, str] | None]:
    """Process one Salesforce opportunity dict.

    Returns (updated_delta, untracked_delta, error_delta, untracked_opp_or_None).

    implementation change: when an open opportunity has no matching local pursuit file, the
    opp dict is returned as the fourth element so the caller can accumulate and
    display a formatted table of untracked opportunities.  Closed opportunities
    and successfully-written opportunities return None as the fourth element.
    """
    opp_id = opp.get("opportunity_id", "")
    opp_name = opp.get("name", "") or ""
    if not opp_id:
        _error("Salesforce candidate has no opportunity identity; no local data was written.")
        return 0, 0, 1, None

    try:
        pursuit_file = match_pursuit(Path(pursuit_dir), opp_id, workspace=get_fieldkit_home())
    except AuthError:
        raise
    except (FieldkitError, OSError) as exc:
        _error(f"matching {opp_id}: {exc}")
        return 0, 0, 1, None

    if not pursuit_file:
        stage = opp.get("stage", "")
        if stage and stage.startswith("Closed"):
            return 0, 0, 0, None
        _log(f"  UNTRACKED: {opp_id} — {opp_name[:60]}")
        # implementation change: return the opp dict so the caller can show a formatted table.
        return 0, 1, 0, opp

    write_data = dict(opp)
    write_data["status"] = "ok"
    write_data.setdefault("pulled_at", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    try:
        sync_opportunity(opp_id, pursuit_file, write_data, dry_run=not write)
        _log(f"  {'✓' if write else 'WOULD UPDATE:'} {opp_id} → {pursuit_file}")
        return 1, 0, 0, None
    except AuthError:
        raise
    except (FieldkitError, OSError) as exc:
        _error(f"writing {opp_id} → {pursuit_file}: {exc}")
        return 0, 0, 1, None


def _resolve_territory_target(territory: str | None, target: str) -> str:
    """Resolve --territory to an account name. Returns updated target."""
    if not territory:
        return target
    from fieldkit.config import get_territory_account_map

    terr_map = get_territory_account_map()
    if territory not in terr_map:
        # historic regression: show code→slug pairs so the user sees human-readable account names,
        # not opaque internal territory codes (e.g. FSI_SOUTH_TERR03).
        known = sorted(f"{k} ({v})" for k, v in terr_map.items())
        click.echo(f"ERROR: unknown territory {territory!r}. Known: {known}", err=True)
        raise SystemExit(EXIT_DATA)
    return terr_map[territory]


def _check_sf_auth() -> tuple[str, str]:
    """Validate SF session and base URL. Returns (sid, base_url) or exits."""
    _log("Checking Salesforce session...")
    sid = get_sf_session_id()
    if sid is None:
        _error(f"No Salesforce session cookie found. {_reauth_hint()}")
        raise SystemExit(EXIT_AUTH)

    base_url = get_sf_rest_base_url()
    if not base_url:
        _error("Could not determine SF REST base URL. Check accounts.yaml sf.org_url.")
        raise SystemExit(EXIT_AUTH)

    _log("Session active.")
    return sid, base_url


def _sync_account_opps(
    account: str,
    accounts_cfg: dict[str, Any],
    root: Any,
    client: Any,
    *,
    services_only: bool = False,
    limit: int = 50,
    dry_run: bool = False,
) -> tuple[int, int, int, bool, list[dict[str, str]]]:
    """Sync opportunities for one account. Returns (updated, untracked, errors, had_auth_error, untracked_opps)."""

    keywords: list[str] = accounts_cfg.get(account, {}).get("keywords", [])
    if not keywords:
        _error(f"No keywords configured for {account} in accounts.yaml — cannot SOSL search.")
        return 0, 0, 1, False, []

    _log(f"SOSL search for {account!r} using keywords: {keywords}")
    try:
        if services_only:
            candidate_result = client.search_opportunity_candidates(keywords=keywords, account_name=account)
            if candidate_result.capped:
                _error(
                    f"Salesforce returned its 2000-record maximum for {account}; "
                    "the total is unknown. Refine account keywords or select a narrower target."
                )
                return 0, 0, 1, False, []
            results = candidate_result.records
            if len(results) > limit:
                _error(
                    f"{account} has {len(results)} candidates, above --limit {limit}. "
                    f"Rerun with --limit {len(results)} or select a narrower target."
                )
                return 0, 0, 1, False, []
        else:
            results = client.search_opportunities(keywords=keywords, account_name=account)
    except sf_errors.SFAuthError as exc:
        # historic regression: don't sys.exit(2) here — that kills all remaining accounts.
        _error(f"Auth failure for {account}: {exc}. {_reauth_hint()}")
        return 0, 0, 0, True, []
    except sf_errors.SFAPIError as exc:
        _error(f"SOSL search failed for {account}: {exc}")
        return 0, 0, 1, False, []

    _log(f"SOSL found {len(results)} opportunity/ies for {account!r}.")
    opps: list[dict[str, Any]] = results
    total_errors = 0
    if services_only:
        from fieldkit.sf.components import fetch_opp_component_lines

        qualified: list[dict[str, Any]] = []
        for opp in opps:
            opp_id = opp.get("opportunity_id")
            if not opp_id:
                _error("component scan skipped a candidate with no opportunity id")
                total_errors += 1
                continue
            try:
                lines = fetch_opp_component_lines(client, str(opp_id))
            except sf_errors.SFAuthError:
                raise
            except sf_errors.SFAPIError as exc:
                _error(f"component scan failed for {opp_id}: {exc}")
                total_errors += 1
                continue
            if any(line["bucket"] != "product" for line in lines):
                qualified.append(opp)
        opps = qualified
    _log(f"Found {len(opps)} opportunities.")

    account_updated = 0
    account_untracked = 0
    pursuit_dir = str(root / "accounts" / account / "pursuits")
    untracked_opps: list[dict[str, str]] = []

    for opp in opps:
        if dry_run:
            u, unt, err, untracked_opp = _process_opp(opp, pursuit_dir, write=False)
        else:
            u, unt, err, untracked_opp = _process_opp(opp, pursuit_dir)
        account_updated += u
        account_untracked += unt
        total_errors += err
        if untracked_opp is not None:
            untracked_opps.append(untracked_opp)

    update_label = "would_update" if dry_run else "updated"
    _log(f"{account}: {update_label}={account_updated} untracked={account_untracked}")
    return account_updated, account_untracked, total_errors, False, untracked_opps


def _run_listview(
    target: str,
    territory: str | None = None,
    *,
    as_json: bool = False,
    quiet: bool = False,
    services_only: bool = False,
    limit: int = 50,
    dry_run: bool = False,
) -> None:
    """Core listview logic — SOSL-only Salesforce opportunity discovery and frontmatter sync."""
    import fieldkit.sf.client as _sf_direct

    # implementation change: resolve --territory to an account name before any other processing
    target = _resolve_territory_target(territory, target)

    sid, base_url = _check_sf_auth()

    accounts_cfg = get_accounts_config(strict=True).get("accounts", {})
    known_accounts = configured_accounts()
    account_names = known_accounts if target == "--all" else (target,)
    if any(account not in known_accounts for account in account_names):
        raise FieldkitError("Salesforce account is not configured")

    root = get_fieldkit_home()
    total_updated = 0
    total_untracked = 0
    total_errors = 0
    had_auth_error = False
    all_untracked_opps: list[dict[str, str]] = []

    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        for account in account_names:
            # Skip accounts marked internal: true — they are internal tracking
            # accounts, not customer accounts with SF opportunities. Querying SF
            # with their keywords surfaces peer-account deals not owned by this AE.
            if accounts_cfg.get(account, {}).get("internal", False):
                _log(f"── {account} (internal — skipped)")  # pii-guard: ignore
                continue
            click.echo("", err=True)
            _log(f"── {account} ─────────────────────────────")
            u, unt, err, auth_err, untracked = _sync_account_opps(
                account,
                accounts_cfg,
                root,
                client,
                services_only=services_only,
                limit=limit,
                dry_run=dry_run,
            )
            total_updated += u
            total_untracked += unt
            total_errors += err
            if auth_err:
                had_auth_error = True
            all_untracked_opps.extend(untracked)

    # historic regression: deferred auth-error exit — all accounts were processed before we exit.
    if had_auth_error:
        _error(f"One or more accounts had auth failures. {_reauth_hint()}")
        raise SystemExit(EXIT_AUTH)

    click.echo("", err=True)
    update_label = "would_update" if dry_run else "updated"
    _log(f"Done. {update_label}={total_updated} untracked={total_untracked} errors={total_errors}")

    # implementation change: print a formatted table of untracked open opportunities
    if not quiet:
        print_untracked_table(all_untracked_opps, _log)
    print_sync_summary(
        total_updated,
        total_untracked,
        total_errors,
        as_json=as_json,
        quiet=quiet,
        dry_run=dry_run,
    )
    if total_errors:
        raise SystemExit(EXIT_PARTIAL)


# ── Click CLI ─────────────────────────────────────────────────────────────────


def _validate_territory(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """historic regression: reject values that look like flags (start with '-').

    Click eagerly consumes --help as the value for --territory when the user
    types ``--territory --help``.  This callback detects that case and raises
    BadParameter so the user sees a clear error instead of a confusing lookup
    failure.  To see help, run the command without --territory first.
    """
    if value is not None and value.startswith("-"):
        raise click.BadParameter(
            f"{value!r} looks like a flag, not a territory name. "
            "Run `fieldkit sf listview --help` (without --territory) to see usage.",
            ctx=ctx,
            param=param,
        )
    return value


@declare_write("workspace")
@click.command("listview")
@click.argument("target", default=None, required=False)
@click.option(
    "--all",
    "sync_all",
    is_flag=True,
    default=False,
    help="Sync all accounts (default when no TARGET is given).",
)
@click.option(
    "--territory",
    default=None,
    metavar="TERRITORY",
    callback=_validate_territory,
    help="Salesforce territory name (sf_territory in accounts.yaml). Resolves to the matching account.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON summary to stdout.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress [sf-listview-sync] progress lines.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Read and match Salesforce opportunities without writing cache or frontmatter.",
)
@click.option(
    "--services-only",
    is_flag=True,
    default=False,
    help="Qualify services opportunities from CPQ component lines, including TAM.",
)
@click.option(
    "--limit", type=click.IntRange(min=1), default=None, help="Maximum candidates per services scan (default: 50)."
)
def cli(
    target: str | None,
    sync_all: bool,
    territory: str | None,
    as_json: bool,
    quiet: bool,
    dry_run: bool,
    services_only: bool,
    limit: int | None,
) -> None:
    """Fetch and display Salesforce list view records.

    TARGET is a configured account slug (e.g. acme-corp).
    Use --all to sync all accounts (default when no TARGET given).
    --territory overrides TARGET by resolving the sf_territory value to an account name.

    Exit codes:
      0 — sync completed without errors
      1 — partial failure (some accounts had errors)
      2 — auth failure
    """
    if limit is not None and not services_only:
        raise click.BadParameter("--limit requires --services-only", param_hint="--limit")

    # implementation change: require explicit --account or --all; warn if neither provided
    if target is None and not sync_all and territory is None:
        click.echo(
            "Warning: no account specified. Syncing all accounts. "
            "Use --all to suppress this warning, or pass an account name.",
            err=True,
        )
        resolved_target = "--all"
    elif sync_all or target is None:
        resolved_target = "--all"
    else:
        resolved_target = target

    global _quiet_mode  # noqa: PLW0603
    _quiet_mode = quiet
    _run_listview(
        resolved_target,
        territory=territory,
        as_json=as_json,
        quiet=quiet,
        services_only=services_only,
        limit=limit if limit is not None else 50,
        dry_run=dry_run,
    )
