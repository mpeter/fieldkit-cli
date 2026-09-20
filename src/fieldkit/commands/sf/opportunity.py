"""sf_pipeline opportunity — Sync a single Salesforce opportunity to a pursuit file.

Fetches full opportunity data via the REST API (not SOSL), including:
  - Stage, Close Date, Owner
  - ACV, ARR, Consulting/Training/Application/Services totals
  - Next Steps (Next_Steps__c)
  - MEDDPICC-adjacent fields: Identify Pain, Decision Criteria, Main Competitor

Usage:
    fieldkit sf opportunity <opp_id> [<pursuit_file>]

If pursuit_file is omitted, resolves it automatically via match-pursuit
across all account pursuit directories. If no match, prints UNTRACKED.
"""

import json
from datetime import UTC, datetime
from typing import Any

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import (
    get_accounts_config,
    get_fieldkit_home,
    get_sf_rest_base_url,
    get_sf_session_id,
)
from fieldkit.sf.client import SFAuthError, SFDirectClient
from fieldkit.sf.components import ContractType, opp_contract_type
from fieldkit.sf.opportunities import is_opportunity_number, resolve_opportunity_reference
from fieldkit.sf.types import DealSplitRecord, OpportunitySObject

LOG_PREFIX = "[sf-opportunity]"

# Fields to request from the Opportunity sobject
_OPP_FIELDS = ",".join(
    [
        "Id",
        "Name",
        "StageName",
        "CloseDate",
        "IsClosed",
        "Owner.Name",
        "Owner.Email",
        "ACV_Opportunity_USD__c",
        "ARR_Opportunity_USD__c",
        "Consulting_Total_USD__c",
        "Training_Total_USD__c",
        "Application_Services_Total_USD__c",
        "Services_Total_USD__c",
        "Next_Steps__c",
        "Customer_Pain_Point__c",
        "Identify_Pain_Long__c",
        "Decision_Criteria__c",
        "Main_Competitor__c",
        "Closed_Lost_Reason__c",
        "Account.Id",
        "Account.Name",
        "Account.Industry",
        "Probability",
        "OpportunityNumber__c",
    ]
)


def _log(msg: str) -> None:
    click.echo(f"{LOG_PREFIX} {msg}", err=True)


def _fmt_currency(val: float | None) -> str:
    """Format a float dollar value as a readable string.

    Returns an empty string when val is None (SF null), so callers always
    receive a str and frontmatter writers never store a None for currency fields.
    """
    if val is None:
        return ""
    if val == 0.0:
        return "$0"
    return f"${val:,.0f}"


def _fetch_deal_splits(opp_id: str) -> list[DealSplitRecord]:
    """Fetch deal splits for an opportunity, returning [] on any error."""
    import logging

    import fieldkit.sf.client as _sf_direct

    sid = get_sf_session_id()
    if not sid:
        return []

    base_url = get_sf_rest_base_url()
    if not base_url:
        return []

    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        try:
            return client.fetch_deal_splits(opp_id)
        except _sf_direct.SFAuthError:
            return []
        except _sf_direct.SFAPIError as exc:
            logging.getLogger(__name__).warning("%s Deal splits unavailable: %s", LOG_PREFIX, exc)
            return []


def _fetch_contract_type(opp_id: str) -> ContractType:
    """Derive contract type from the opp's quote-line families (historic regression).

    Opens its own client (mirrors ``_fetch_deal_splits``). Degrades to
    ``"standard"`` when no session is configured; ``opp_contract_type`` itself
    degrades non-auth walk failures to ``"standard"`` and re-raises
    ``SFAuthError`` — which must propagate to ``cli_main()`` (exit 2).
    """
    import fieldkit.sf.client as _sf_direct

    sid = get_sf_session_id()
    if not sid:
        return "standard"

    base_url = get_sf_rest_base_url()
    if not base_url:
        return "standard"

    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        return opp_contract_type(client, opp_id)


def _fetch_opportunity(opp_id: str) -> OpportunitySObject:
    """Fetch a single Opportunity from SF REST API with all relevant fields."""
    import fieldkit.sf.client as _sf_direct

    sid = get_sf_session_id()
    if not sid:
        _log("ERROR: No Salesforce session. Run: fieldkit auth sf")
        raise SystemExit(EXIT_AUTH)

    base_url = get_sf_rest_base_url()
    if not base_url:
        _log("ERROR: No Salesforce base URL configured.")
        raise SystemExit(EXIT_AUTH)

    _log(f"Fetching opportunity {opp_id} from Salesforce...")
    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        try:
            return client.fetch_record(opp_id, fields=_OPP_FIELDS)
        except _sf_direct.SFAuthError:
            _log("ERROR: Auth failure. Run: fieldkit auth sf")
            raise
        except _sf_direct.SFNotFoundError:
            _log(f"ERROR: Opportunity {opp_id} not found in Salesforce.")
            raise SystemExit(EXIT_DATA) from None
        except _sf_direct.SFAPIError as exc:
            _log(f"ERROR: {exc}")
            raise SystemExit(EXIT_PARTIAL) from None


def _resolve_pursuit_file(opp_id: str) -> str | None:
    """Search all account pursuit directories for a matching pursuit file."""
    import contextlib
    import io

    from fieldkit.commands.sf.sync import do_match_pursuit

    try:
        data_root = get_fieldkit_home()
    except Exception:  # noqa: BLE001
        return None

    accounts_cfg = get_accounts_config().get("accounts", {})
    for _acct_name, info in accounts_cfg.items():
        if not isinstance(info, dict):
            continue
        pursuit_dir = info.get("pursuit_dir")
        if not pursuit_dir:
            continue
        full_dir = str(data_root / pursuit_dir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_match_pursuit(full_dir, opp_id)
        result = buf.getvalue().strip()
        if result:
            return result
    return None


def _resolve_numeric_opportunity(opp_number: str) -> str | None:
    """Resolve a validated numeric opportunity reference with configured auth."""
    sid = get_sf_session_id()
    base_url = get_sf_rest_base_url()
    if not sid or not base_url:
        raise SFAuthError("Salesforce authentication is not configured; run 'fieldkit auth sf'")
    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        return resolve_opportunity_reference(client, opp_number)


def _build_write_payload(rec: OpportunitySObject, contract_type: ContractType = "standard") -> dict[str, Any]:
    """Build a write-opp compatible payload from a raw SF opportunity record."""
    owner = rec.get("Owner") or {}
    account = rec.get("Account") or {}

    consulting = rec.get("Consulting_Total_USD__c")
    training = rec.get("Training_Total_USD__c")
    app_services = rec.get("Application_Services_Total_USD__c")
    services_total = rec.get("Services_Total_USD__c")
    acv = rec.get("ACV_Opportunity_USD__c")
    arr = rec.get("ARR_Opportunity_USD__c")

    return {
        "status": "ok",
        "opportunity_id": rec.get("Id"),
        "name": rec.get("Name"),
        "stage": rec.get("StageName"),
        "close_date": str(rec.get("CloseDate")) if rec.get("CloseDate") else None,
        "arr": _fmt_currency(arr),
        "owner": owner.get("Name"),
        "owner_email": owner.get("Email"),
        "next_steps": rec.get("Next_Steps__c"),
        "acv": _fmt_currency(acv),
        "consulting_acv": _fmt_currency(consulting),
        "training_acv": _fmt_currency(training),
        "contract_type": contract_type,
        "application_services_acv": _fmt_currency(app_services),
        "services_total": services_total,
        "sf_account_name": account.get("Name"),
        "sf_account_industry": account.get("Industry"),
        "sf_account_sf_id": account.get("Id"),
        # MEDDPICC-adjacent signals from SF
        "sf_identify_pain": rec.get("Identify_Pain_Long__c") or rec.get("Customer_Pain_Point__c"),
        "sf_decision_criteria": rec.get("Decision_Criteria__c"),
        "sf_main_competitor": rec.get("Main_Competitor__c"),
        "sf_closed_lost_reason": rec.get("Closed_Lost_Reason__c"),
        "sf_probability": rec.get("Probability"),
        "opportunity_number": str(rec.get("OpportunityNumber__c"))
        if rec.get("OpportunityNumber__c") is not None
        else None,
        "pulled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _print_summary_header(rec: OpportunitySObject) -> None:
    """Print the opportunity header block (name, ID, stage, owner, account)."""
    owner = rec.get("Owner") or {}
    account = rec.get("Account") or {}
    click.echo(f"\n{'=' * 60}")
    click.echo(f"  {rec.get('Name', 'Unknown')}")
    click.echo(f"{'=' * 60}")
    click.echo(f"  ID:           {rec.get('Id')}")
    click.echo(f"  Stage:        {rec.get('StageName')}")
    click.echo(f"  Close Date:   {rec.get('CloseDate')}")
    click.echo(f"  Owner:        {owner.get('Name')} <{owner.get('Email')}>")
    click.echo(f"  Account:      {account.get('Name')} ({account.get('Industry')})")
    click.echo()


def _print_financials(rec: OpportunitySObject) -> None:
    """Print the Services Financials section."""
    consulting = rec.get("Consulting_Total_USD__c") or 0.0
    training = rec.get("Training_Total_USD__c") or 0.0
    app_services = rec.get("Application_Services_Total_USD__c") or 0.0
    services_total = rec.get("Services_Total_USD__c") or 0.0
    click.echo("  ── Services Financials ──")
    click.echo(f"  ACV:              {_fmt_currency(rec.get('ACV_Opportunity_USD__c'))}")
    click.echo(f"  ARR:              {_fmt_currency(rec.get('ARR_Opportunity_USD__c'))}")
    click.echo(f"  Services Total:   {_fmt_currency(services_total)}")
    click.echo(f"    Consulting:     {_fmt_currency(consulting)}")
    click.echo(f"    Training:       {_fmt_currency(training)}")
    click.echo(f"    App Services:   {_fmt_currency(app_services)}")
    click.echo()


def _print_meddpicc_signals(rec: OpportunitySObject) -> None:
    """Print the MEDDPICC Signals and Next Steps sections."""
    pain = rec.get("Identify_Pain_Long__c") or rec.get("Customer_Pain_Point__c")
    decision_criteria = rec.get("Decision_Criteria__c")
    competitor = rec.get("Main_Competitor__c")
    next_steps = rec.get("Next_Steps__c")
    click.echo("  ── MEDDPICC Signals (from SF) ──")
    click.echo(f"  Identify Pain:      {pain or '(not set)'}")
    click.echo(f"  Decision Criteria:  {decision_criteria or '(not set)'}")
    click.echo(f"  Main Competitor:    {competitor or '(not set)'}")
    click.echo()
    click.echo("  ── Next Steps ──")
    click.echo(f"  {next_steps or '(not set)'}")
    click.echo()


def _print_deal_splits(splits: list[DealSplitRecord]) -> None:
    """Print the Deal Splits section."""
    click.echo("  ── Deal Splits ──")
    max_label = max(len(s.get("offering_group", "")) for s in splits)
    col = max(max_label, 10)
    for s in splits:
        label = s.get("offering_group", "")
        pct = s.get("services_pct", 0.0)
        click.echo(f"  {label:<{col}}   {pct:.0f}%")
    click.echo()


def _print_summary(
    rec: OpportunitySObject, pursuit_file: str | None, splits: list[DealSplitRecord] | None = None
) -> None:
    """Print a human-readable summary of the opportunity."""
    _print_summary_header(rec)
    _print_financials(rec)
    _print_meddpicc_signals(rec)

    if splits:
        _print_deal_splits(splits)

    if rec.get("Closed_Lost_Reason__c"):
        click.echo(f"  Closed Lost Reason: {rec.get('Closed_Lost_Reason__c')}")
        click.echo()

    if pursuit_file:
        click.echo(f"  Local pursuit:  {pursuit_file}")
    else:
        click.echo("  Local pursuit:  UNTRACKED")
    click.echo()


def run_opportunity(opp_id: str, pursuit_file: str | None, *, write: bool = True) -> int:
    """Core logic for opportunity sync.

    Returns exit code (0=success, 1=error, 2=auth, 3=data).
    """
    import logging

    from fieldkit.commands.sf.sync import PLACEHOLDER_VALUES, _validate_opp_id

    if opp_id.strip().lower() in PLACEHOLDER_VALUES:
        logging.warning(
            "sf_opportunity_id %r is a placeholder — skipping sync",
            opp_id,
        )
        return 0

    if not _validate_opp_id(opp_id):
        logging.warning(
            "sf_opportunity_id %r is not a valid Salesforce ID (expected 15 or 18 alphanumeric chars) — skipping sync",
            opp_id,
        )
        return 3

    rec = _fetch_opportunity(opp_id)
    splits = _fetch_deal_splits(opp_id)

    # Resolve pursuit file if not given
    if not pursuit_file:
        pursuit_file = _resolve_pursuit_file(opp_id)

    _print_summary(rec, pursuit_file, splits=splits)

    if not write:
        return 0

    if not pursuit_file:
        # historic regression: exit 3 (data error) with a clear user-facing message so the
        # operator knows the opportunity is untracked and can take action.
        # Previously returned 0 (success), which silently swallowed the miss.
        click.echo(
            f"ERROR: {opp_id} is not tracked — no matching pursuit file found. "
            "Create a pursuit file and link it with the sf_opportunity_id frontmatter key.",
            err=True,
        )
        return 3

    contract_type = _fetch_contract_type(opp_id)
    payload = _build_write_payload(rec, contract_type)
    # Attach deal splits (already fetched above) — map to frontmatter shape
    payload["deal_splits"] = [
        {"offering": s.get("offering_group", ""), "pct": s.get("services_pct", 0.0)} for s in splits
    ]
    from fieldkit.commands.sf.sync import do_write_opp

    do_write_opp(opp_id, pursuit_file, json.dumps(payload))
    _log(f"✓ Written: {pursuit_file}")
    return 0


@click.command(
    name="opportunity",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("opp_id")
@click.argument("pursuit_file", required=False, default=None)
@click.option("--no-write", is_flag=True, default=False, help="Print summary without updating frontmatter.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output (suppresses human table).",
)
def cli(opp_id: str, pursuit_file: str | None, no_write: bool, as_json: bool) -> None:
    """Fetch and sync a single Salesforce opportunity to a pursuit file.

    OPP_ID is the 15- or 18-character Salesforce opportunity ID, or a numeric
    Salesforce Opportunity Number (e.g. 71721820) which will be resolved to
    the 18-char ID via SOSL before fetching.

    PURSUIT_FILE is optional — if omitted, auto-resolved via match-pursuit.

    Fetches: stage, close date, owner, ACV/ARR, consulting/training/app services
    splits, next steps, and MEDDPICC-adjacent signals (identify pain, decision
    criteria, main competitor).
    """
    # Resolve opportunity number (all-digits, 5-12 chars) to 18-char record Id.
    if is_opportunity_number(opp_id.strip()):
        _log(f"Resolving opportunity number {opp_id!r} to Salesforce record Id...")
        resolved = _resolve_numeric_opportunity(opp_id.strip())
        if not resolved:
            _log(
                f"ERROR: Could not resolve opportunity number {opp_id!r} to a Salesforce record. "
                "Verify the number is correct and that OpportunityNumber__c is available in the configured organization."
            )
            raise SystemExit(EXIT_DATA)
        _log(f"Resolved {opp_id!r} → {resolved}")
        opp_id = resolved

    if as_json:
        # cell-28b9dae2e9395288: machine-readable output.
        from fieldkit.commands.sf.sync import PLACEHOLDER_VALUES, _validate_opp_id

        if opp_id.strip().lower() in PLACEHOLDER_VALUES or not _validate_opp_id(opp_id):
            _log(f"ERROR: Invalid or placeholder opportunity ID: {opp_id!r}")
            raise SystemExit(EXIT_DATA)
        rec = _fetch_opportunity(opp_id)
        splits = _fetch_deal_splits(opp_id)
        contract_type = _fetch_contract_type(opp_id)
        payload = _build_write_payload(rec, contract_type)
        payload["deal_splits"] = [
            {"offering": s.get("offering_group", ""), "pct": s.get("services_pct", 0.0)} for s in splits
        ]
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    code = run_opportunity(opp_id, pursuit_file, write=not no_write)
    raise SystemExit(code)
