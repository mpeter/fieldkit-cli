"""sf_pipeline account — Fetch Salesforce account overview and services pipeline.

Pulls the Account record from Salesforce plus a live SOSL search for all
services opportunities, and prints a consolidated account dashboard:
  - Account metadata (owner, segment, industry, location)
  - Open services pipeline: stage, ACV, consulting/training splits
  - Aggregate services ACV across all tracked open opportunities
  - Writes account frontmatter to accounts/<name>/account.md

Usage:
    fieldkit sf account <account_name>

account_name matches a key in accounts.yaml (e.g. acme, globalpay, midwestins).
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

import fieldkit.sf.client as _sf_direct
from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import (
    get_account_names,
    get_accounts_config,
    get_fieldkit_home,
    get_sf_rest_base_url,
    get_sf_session_id,
)
from fieldkit.pursuit.io import extract_frontmatter_text
from fieldkit.pursuit.stages import CLOSED_STAGES as _CLOSED_STAGES
from fieldkit.sf.client import SFAPIError as _SFAPIError
from fieldkit.sf.client import reauth_hint_message as _reauth_hint
from fieldkit.sf.components import effective_net_consulting_acv, opp_contract_type

LOG_PREFIX = "[sf-account]"

# Matches valid 15- or 18-character Salesforce IDs (alphanumeric only).
# Rejects placeholder strings like "NEEDS-LOOKUP", "TBD", etc.
# Pattern mirrors OPP_ID_RE in fieldkit/sf_pipeline/sync.py.
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15}$|^[A-Za-z0-9]{18}$")

_ACCOUNT_FIELDS = ",".join(
    [
        "Id",
        "Name",
        "Industry",
        "Owner.Name",
        "Owner.Email",
        "BillingCity",
        "BillingState",
        "BillingCountry",
        "Type",
        "Account_Segment__c",
        "Account_Subsegment__c",
        "AnnualRevenue",
        "NumberOfEmployees",
    ]
)


def _log(msg: str) -> None:
    click.echo(f"{LOG_PREFIX} {msg}", err=True)


def _fmt_currency(val: float | None) -> str:
    if val is None:
        return "(not set)"
    if val == 0.0:
        return "$0"
    return f"${val:,.0f}"


def _resolve_account_id_by_keywords(account_name: str, info: dict[str, Any], client: Any) -> str | None:
    """Strategy 1: SOSL keyword search — fast, no local file I/O.

    Returns the Salesforce Account ID, or None if not found.
    """
    keywords: list[str] = info.get("keywords", []) if isinstance(info, dict) else []
    if not keywords:
        return None
    acct_id: str | None = client.resolve_account_id_by_keywords(keywords=keywords, account_name=account_name)
    if acct_id:
        return acct_id
    _log(f"SOSL keyword search returned no Account.Id for '{account_name}', trying pursuit file scan.")
    return None


# implementation note: Salesforce SOSL queries have a ~10K character limit.
# At ~18 chars per ID + " OR " separators, 200 IDs ~= 4KB — safely within the limit.
_SOSL_CHUNK_SIZE = 200


def _resolve_account_id_by_pursuit_scan(account_name: str, info: dict[str, Any], client: Any) -> str | None:
    """Strategy 2: Scan local pursuit files for a tracked sf_opportunity_id.

    implementation note: Refactored from N sequential fetch_record() calls (one per pursuit file)
    to a single batch sosl_search() call. IDs are chunked to stay within the
    Salesforce SOSL 10K character limit (_SOSL_CHUNK_SIZE = 200 per batch).

    Used when the keyword search returns no results (e.g. keywords too generic).
    Returns the Salesforce Account ID, or None if not found.
    """

    try:
        data_root = get_fieldkit_home()
    except Exception:  # noqa: BLE001
        return None

    pursuit_dir_rel = info.get("pursuit_dir", f"accounts/{account_name}/pursuits")
    pursuit_dir = data_root / pursuit_dir_rel

    if not pursuit_dir.is_dir():
        return None

    # Phase 1: collect all valid opp IDs from pursuit files (no API calls).
    opp_ids: list[str] = []
    for md in sorted(pursuit_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm_text = extract_frontmatter_text(text)
        if not fm_text:
            continue
        try:
            fm = yaml.safe_load(fm_text)
        except Exception:  # noqa: BLE001
            continue
        opp_id = fm.get("sf_opportunity_id") if isinstance(fm, dict) else None
        if not opp_id or not _SF_ID_RE.fullmatch(str(opp_id)):
            if opp_id:
                _log(f"WARNING: skipping invalid sf_opportunity_id {opp_id!r} in {md}")
            continue
        opp_ids.append(str(opp_id))

    if not opp_ids:
        return None

    # Phase 2: batch SOSL search in chunks of _SOSL_CHUNK_SIZE.
    return _sosl_search_for_account_id(opp_ids, client)


def _sosl_search_for_account_id(opp_ids: list[str], client: Any) -> str | None:
    """Search for an Account ID via SOSL, batching opp IDs in chunks of _SOSL_CHUNK_SIZE.

    IN ALL FIELDS is correct for Salesforce record ID lookups (not keyword searches).
    SFAuthError propagates; SFAPIError is caught per-chunk so remaining chunks are tried.

    Args:
        opp_ids: Valid Salesforce Opportunity IDs to look up.
        client:  SF client exposing sosl_search(sosl_query) → list[dict].

    Returns:
        First AccountId found, or None if no chunk returns a result.
    """

    for i in range(0, len(opp_ids), _SOSL_CHUNK_SIZE):
        chunk = opp_ids[i : i + _SOSL_CHUNK_SIZE]
        id_list = " OR ".join(chunk)
        sosl = f"FIND {{{id_list}}} IN ALL FIELDS RETURNING Opportunity(AccountId)"
        try:
            records = client.sosl_search(sosl)
            for opp in records:
                acct_id = opp.get("AccountId")
                if acct_id:
                    return str(acct_id)
        except _SFAPIError:
            continue

    return None


def _resolve_sf_account_id(account_name: str, client: Any, base_url: str) -> str | None:
    """Resolve the Salesforce Account ID for an account.

    Tries two strategies in order:

    1. **SOSL keyword search** (fast — single API call): searches for opportunities
       matching the account's configured keywords and extracts Account.Id from the
       first result.  Works for all accounts with keywords configured.

    2. **Local pursuit file scan** (fallback): reads local pursuit files to find
       a tracked sf_opportunity_id, then fetches that opportunity to get Account.Id.
       Used when the keyword search returns no results (e.g. keywords too generic).
    """
    accounts_cfg = get_accounts_config().get("accounts", {})
    info = accounts_cfg.get(account_name, {})
    if not isinstance(info, dict):
        return None

    acct_id = _resolve_account_id_by_keywords(account_name, info, client)
    if acct_id:
        return acct_id

    return _resolve_account_id_by_pursuit_scan(account_name, info, client)


def _fetch_account_record(account_id: str, client: Any, base_url: str) -> dict[str, Any] | None:
    """Fetch the Account record from SF."""

    try:
        result: dict[str, Any] = client.fetch_sobject("Account", account_id, _ACCOUNT_FIELDS)
        return result
    except _sf_direct.SFNotFoundError:
        _log(f"WARNING: Account {account_id} not found in Salesforce.")
        return None
    except _sf_direct.SFAPIError as exc:
        _log(f"WARNING: Account fetch failed: {exc}")
        return None


def _fetch_net_acv(opp_id: str, client: Any) -> float | None:
    """Fetch an opp's net ACV (``ACV_Opportunity_USD__c``) for the FP re-anchor.

    Returns None (degraded) on a non-auth error; ``SFAuthError`` propagates.
    """
    try:
        rec = client.fetch_record(opp_id, fields="ACV_Opportunity_USD__c")
    except _sf_direct.SFAuthError:
        raise
    except (_sf_direct.SFNotFoundError, _SFAPIError) as exc:
        _log(f"WARNING: net ACV fetch failed for {opp_id}: {exc} — using gross")
        return None
    val = rec.get("ACV_Opportunity_USD__c")
    return float(val) if isinstance(val, (int, float)) else None


def _enrich_contract_type(opp: dict[str, Any], client: Any) -> None:
    """Annotate one open opp dict with its derived ``contract_type`` (historic regression).

    Bounded per-opp quote-line walk (design D4): a non-auth walk failure degrades
    to ``"standard"`` inside ``opp_contract_type``; ``SFAuthError`` propagates.
    For a fixed-price opp lacking a live net ACV, fetch ``ACV_Opportunity_USD__c``
    so the open-pipeline totals can re-anchor to net rather than gross.
    """
    opp_id = str(opp.get("opportunity_id") or opp.get("id") or "")
    if not opp_id or not _SF_ID_RE.fullmatch(opp_id):
        opp["contract_type"] = "standard"
        return
    contract_type = opp_contract_type(client, opp_id)
    opp["contract_type"] = contract_type
    if contract_type == "fixed_price" and opp.get("acv") is None:
        opp["acv"] = _fetch_net_acv(opp_id, client)


def _fetch_live_opportunities(account_name: str, client: Any) -> list[dict[str, Any]]:
    """Fetch open services opportunities via SOSL search, annotated with contract type."""
    accounts_cfg = get_accounts_config().get("accounts", {})
    info = accounts_cfg.get(account_name, {})
    keywords: list[str] = info.get("keywords", [account_name]) if isinstance(info, dict) else [account_name]

    opps = client.search_opportunities(keywords=keywords, account_name=account_name)

    open_opps = [o for o in opps if not (o.get("stage") or "").startswith("Closed")]
    for opp in open_opps:
        _enrich_contract_type(opp, client)
    return open_opps


def _print_account_header(account_name: str, acct_rec: dict[str, Any] | None) -> None:
    owner = (acct_rec.get("Owner") or {}) if acct_rec else {}
    click.echo(f"\n{'=' * 60}")
    click.echo(
        f"  {acct_rec.get('Name', account_name) if acct_rec else account_name + ' (account record unavailable)'}"
    )
    click.echo(f"{'=' * 60}")
    if acct_rec:
        location = ", ".join(
            filter(None, [acct_rec.get("BillingCity"), acct_rec.get("BillingState"), acct_rec.get("BillingCountry")])
        )
        click.echo(f"  SF ID:        {acct_rec.get('Id')}")
        click.echo(f"  Type:         {acct_rec.get('Type')}")
        click.echo(f"  Industry:     {acct_rec.get('Industry')}")
        click.echo(f"  Segment:      {acct_rec.get('Account_Segment__c')} / {acct_rec.get('Account_Subsegment__c')}")
        click.echo(f"  Location:     {location or '(not set)'}")
        click.echo(f"  Owner:        {owner.get('Name')} <{owner.get('Email')}>")
    click.echo()


def _collect_local_opp_ids(pursuit_dir: Path) -> frozenset[str]:
    """Return the set of sf_opportunity_id values found in local pursuit files.

    Reads all .md files in pursuit_dir (excluding known non-pursuit files) and
    collects any non-empty sf_opportunity_id values.  Used to mark SF opps that
    have no corresponding local pursuit file with ``[no local pursuit]``.
    """

    ids: set[str] = set()
    if not pursuit_dir.is_dir():
        return frozenset()
    for pf in pursuit_dir.glob("*.md"):
        if pf.name in {"gmail-intel.md", "template.md"}:
            continue
        try:
            text = pf.read_text(encoding="utf-8")
        except OSError:
            continue
        fm_text = extract_frontmatter_text(text)
        if not fm_text:
            continue
        try:
            fm = yaml.safe_load(fm_text)
        except Exception:  # noqa: BLE001
            continue
        opp_id = fm.get("sf_opportunity_id") if isinstance(fm, dict) else None
        if opp_id and isinstance(opp_id, str) and opp_id.strip():
            ids.add(opp_id.strip())
    return frozenset(ids)


def _slugify_opp_name(name: str) -> str:
    """Slugify an SF opportunity name using the same logic as ``pursuit rename``.

    Lowercases, replaces non-alphanumeric runs with hyphens, and strips leading/
    trailing hyphens — matching the implementation change slug used in frontmatter.py.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _print_pipeline_section(
    open_opps: list[dict[str, Any]],
    local_opp_ids: frozenset[str],
    account_name: str = "",
) -> None:
    """Print the open services pipeline section.

    Marks each opportunity with ``[no local pursuit]`` when its SF opportunity
    ID does not appear in any local pursuit file — making it easy for AEs to
    spot SF opps that haven't been tracked locally yet.

    implementation change: the annotation now includes the exact ``fieldkit pursuit create``
    command so users know how to create the missing local pursuit file.
    """
    click.echo(f"  ── Open Services Pipeline ({len(open_opps)} opportunities) ──")
    total_consulting = total_training = 0.0
    if open_opps:
        # historic regression: sort/roll up on the re-anchored consulting value so fixed-price
        # opps contribute net ACV (~½ the gross consulting_acv), not the overstated gross.
        def consulting_acv(opp: dict[str, Any]) -> float:
            return effective_net_consulting_acv(
                opp.get("contract_type"), opp.get("consulting_acv"), opp.get("acv"), opp.get("arr")
            )

        for opp in sorted(open_opps, key=consulting_acv, reverse=True):
            consulting = consulting_acv(opp)
            training = float(opp.get("training_acv") or 0.0)
            total_consulting += consulting
            total_training += training
            stage = opp.get("stage", "?")
            name = (opp.get("name") or "")[:50]
            opp_id = str(opp.get("opportunity_id") or opp.get("id") or "")
            # Mark opps with no matching local pursuit file so AEs can track them.
            # An empty local_opp_ids set (e.g. no pursuit dir) means all opps are unmatched.
            if opp_id and opp_id in local_opp_ids:
                unmatched_tag = ""
            else:
                # implementation change: include the exact create command so the AE can act immediately.
                opp_slug = _slugify_opp_name(opp.get("name") or "")
                acct_flag = f" --account {account_name}" if account_name else ""
                create_cmd = f"fieldkit pursuit create{acct_flag} {opp_slug}" if opp_slug else "fieldkit pursuit create"
                unmatched_tag = f" [no local pursuit — run: {create_cmd}]"
            click.echo(
                f"  {stage:12} | Consulting: {_fmt_currency(consulting):>12} | Training: {_fmt_currency(training):>10} | {name}{unmatched_tag}"
            )
    else:
        click.echo("  (no open services opportunities found via SOSL)")
    click.echo()
    click.echo("  ── Totals ──")
    click.echo(f"  Open Consulting ACV: {_fmt_currency(total_consulting)}")
    click.echo(f"  Open Training ACV:   {_fmt_currency(total_training)}")
    click.echo(f"  Open Services Total: {_fmt_currency(total_consulting + total_training)}")
    click.echo()


def _print_local_pursuits(pursuit_dir: Path) -> None:

    active_files: list[str] = []
    closed_files: list[str] = []
    for pf in sorted(pursuit_dir.glob("*.md")) if pursuit_dir.is_dir() else []:
        if pf.name in {"gmail-intel.md", "template.md"}:
            continue
        try:
            text = pf.read_text(encoding="utf-8")
            # Use extract_frontmatter_text (lib.io) for robust YAML block extraction.
            # The previous text.split("---", 2) was brittle: malformed frontmatter
            # (e.g. missing closing ---) would produce wrong parts, causing closed-lost
            # deals to be classified as active (historic regression).
            fm_text = extract_frontmatter_text(text)
            stage = str((yaml.safe_load(fm_text) or {}).get("stage", "")).lower() if fm_text else ""
        except Exception:  # noqa: BLE001
            stage = ""
        (closed_files if stage in _CLOSED_STAGES else active_files).append(pf.name)
    click.echo(f"  ── Local Pursuits ({len(active_files)} active, {len(closed_files)} closed) ──")
    for name in active_files:
        click.echo(f"  {name}")
    if closed_files:
        click.echo(f"  [closed: {', '.join(closed_files)}]")
    click.echo()


def _print_account_summary(
    account_name: str,
    acct_rec: dict[str, Any] | None,
    open_opps: list[dict[str, Any]],
    pursuit_dir: Path,
) -> None:
    """Print the account dashboard to stdout."""
    _print_account_header(account_name, acct_rec)
    local_opp_ids = _collect_local_opp_ids(pursuit_dir)
    _print_pipeline_section(open_opps, local_opp_ids, account_name=account_name)
    _print_local_pursuits(pursuit_dir)


def _build_account_write_payload(
    account_name: str,
    acct_rec: dict[str, Any] | None,
    open_opps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the write-account payload."""
    owner = (acct_rec.get("Owner") or {}) if acct_rec else {}
    # historic regression: re-anchor fixed-price opps to net ACV before summing (gross ~2x net).
    total_consulting = sum(
        effective_net_consulting_acv(
            opp.get("contract_type"), opp.get("consulting_acv"), opp.get("acv"), opp.get("arr")
        )
        for opp in open_opps
    )
    total_training = sum(o.get("training_acv") or 0.0 for o in open_opps)

    return {
        "status": "ok",
        "account_id": acct_rec.get("Id") if acct_rec else None,
        "account_name": acct_rec.get("Name") if acct_rec else account_name,
        "industry": acct_rec.get("Industry") if acct_rec else None,
        "segment": acct_rec.get("Account_Segment__c") if acct_rec else None,
        "owner": owner.get("Name"),
        "owner_email": owner.get("Email"),
        "open_opportunity_count": len(open_opps),
        "open_consulting_acv": total_consulting if total_consulting else None,
        "open_training_acv": total_training if total_training else None,
        "pulled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def run_account(account_name: str, *, write: bool = True) -> int:
    """Core logic for account sync. Returns exit code."""

    # Validate account name
    valid_names = get_account_names()
    if account_name not in valid_names:
        _log(f"ERROR: Unknown account '{account_name}'. Valid: {', '.join(valid_names)}")
        raise SystemExit(EXIT_DATA)

    sid = get_sf_session_id()
    if not sid:
        _log("ERROR: No Salesforce session. Run: fieldkit auth sf")
        raise SystemExit(EXIT_AUTH)
    base_url = get_sf_rest_base_url()
    if not base_url:
        _log("ERROR: No Salesforce base URL configured.")
        raise SystemExit(EXIT_AUTH)

    accounts_cfg = get_accounts_config().get("accounts", {})
    info = accounts_cfg.get(account_name, {})
    if not isinstance(info, dict):
        _log(f"ERROR: No config found for account '{account_name}'.")
        raise SystemExit(EXIT_DATA)

    try:
        data_root = get_fieldkit_home()
    except Exception as exc:  # noqa: BLE001
        _log(f"ERROR: Could not determine data root: {exc}")
        raise SystemExit(EXIT_PARTIAL) from None

    pursuit_dir_rel = info.get("pursuit_dir", f"accounts/{account_name}/pursuits")
    pursuit_dir = data_root / pursuit_dir_rel

    try:
        with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
            # Resolve SF account ID from a local pursuit file
            _log(f"Resolving SF Account ID for '{account_name}'...")
            sf_account_id = _resolve_sf_account_id(account_name, client, base_url)

            # Guard: if no SF account record found, skip the write to avoid overwriting
            # previously-correct frontmatter with None/empty values (historic regression).
            if sf_account_id is None:
                _log(f"WARNING: no SF account record found for '{account_name}' — skipping frontmatter write")
                return 0

            # Fetch account record
            acct_rec: dict[str, Any] | None = None
            if sf_account_id:
                _log(f"Fetching account record {sf_account_id}...")
                acct_rec = _fetch_account_record(sf_account_id, client, base_url)
            else:
                _log("WARNING: Could not resolve SF Account ID (no tracked pursuits with sf_opportunity_id).")

            # SOSL search for open services opportunities
            _log(f"Searching for open services opportunities for '{account_name}'...")
            open_opps = _fetch_live_opportunities(account_name, client)
            _log(f"Found {len(open_opps)} open services opportunity/ies.")
    except _sf_direct.SFAuthError:
        # historic regression: SFAuthError (HTTP 401) was previously an unhandled exception
        # that printed a raw traceback. Surface it as a clean error + exit 2.
        _log(f"ERROR: Salesforce authentication failed (HTTP 401). Run {_reauth_hint()}")
        raise

    # Print dashboard
    _print_account_summary(account_name, acct_rec, open_opps, pursuit_dir)

    # Write account frontmatter
    if write:
        payload = _build_account_write_payload(account_name, acct_rec, open_opps)
        from fieldkit.commands.sf.sync import do_write_account

        do_write_account(account_name, json.dumps(payload))
        _log(f"✓ Account frontmatter written for '{account_name}'.")

    return 0


@click.command(
    name="account",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("account_name")
@click.option("--no-write", is_flag=True, default=False, help="Print dashboard without updating account.md.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output (suppresses human dashboard).",
)
def cli(account_name: str, no_write: bool, as_json: bool) -> None:
    """Fetch and display the Salesforce account dashboard.

    ACCOUNT_NAME must match a key in accounts.yaml (e.g. acme, globalpay, midwestins).

    Fetches: account metadata (owner, segment, industry), open services pipeline
    with consulting/training splits per opportunity, and aggregate ACV totals.
    Writes results to accounts/<name>/account.md.
    """
    if as_json:
        # cell-28b9dae2e9395288: machine-readable output.
        # run_account with write=False to get data without writing, then emit JSON.
        # We need to capture the payload — call the internal helpers directly.

        valid_names = get_account_names()
        if account_name not in valid_names:
            _log(f"ERROR: Unknown account '{account_name}'. Valid: {', '.join(valid_names)}")
            raise SystemExit(EXIT_DATA)

        sid = get_sf_session_id()
        if not sid:
            _log("ERROR: No Salesforce session. Run: fieldkit auth sf")
            raise SystemExit(EXIT_AUTH)
        base_url = get_sf_rest_base_url()
        if not base_url:
            _log("ERROR: No Salesforce base URL configured.")
            raise SystemExit(EXIT_AUTH)

        try:
            with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
                sf_account_id = _resolve_sf_account_id(account_name, client, base_url)
                acct_rec: dict[str, Any] | None = None
                if sf_account_id:
                    acct_rec = _fetch_account_record(sf_account_id, client, base_url)
                open_opps = _fetch_live_opportunities(account_name, client)
        except _sf_direct.SFAuthError:
            _log(f"ERROR: Salesforce authentication failed (HTTP 401). Run {_reauth_hint()}")
            raise

        payload = _build_account_write_payload(account_name, acct_rec, open_opps)
        payload["opportunities"] = open_opps
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    code = run_account(account_name, write=not no_write)
    raise SystemExit(code)
