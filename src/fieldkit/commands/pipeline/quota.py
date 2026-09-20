"""Pipeline quota collection and Salesforce closed-won fetch helpers.

Pure quota calculation lives in fieldkit.watch.morning_brief_render; the
SF-policy-aware pursuit collector remains here (implementation change step 1).
"""

import calendar
import logging
from datetime import date
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from fieldkit.config import ConfigError
from fieldkit.pursuit import iterate_pursuits
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import load_pursuit
from fieldkit.sf.components import effective_net_consulting_acv

log = logging.getLogger(__name__)

# Maps a quota period's H/Q unit to the calendar month its period ends in.
# Period format is YYYY-H1/H2 or YYYY-Q1..Q4 (see commands/setup/quota.py:_PERIOD_RE).
_PERIOD_END_MONTH: dict[str, int] = {
    "H1": 6,
    "H2": 12,
    "Q1": 3,
    "Q2": 6,
    "Q3": 9,
    "Q4": 12,
}


def get_period_end_date(period: str) -> date:
    """Return the last calendar day of a quota period (e.g. '2026-H1' -> 2026-06-30).

    Raises:
        ValueError: period is not in ``YYYY-H1``/``YYYY-H2``/``YYYY-Q1``..``YYYY-Q4`` format.
    """
    year_str, _, unit = period.partition("-")
    if unit not in _PERIOD_END_MONTH or not year_str.isdigit():
        raise ValueError(f"Unrecognized quota period format: {period!r}")
    year = int(year_str)
    month = _PERIOD_END_MONTH[unit]
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _collect_pursuits_for_quota(
    data_root: Path,
    account_filter: str | None = None,
) -> list[dict[str, object]]:
    """Collect pursuit stage + amount from all (or one account's) pursuit files.

    Args:
        data_root:      Workspace root path.
        account_filter: When set, restricts the scan to
                        ``accounts/<account_filter>/pursuits/``.
    """
    result: list[dict[str, object]] = []

    if account_filter is not None:
        pursuit_paths = sorted((data_root / "accounts" / account_filter / "pursuits").glob("*.md"))
        paths = (p for p in pursuit_paths if ".template" not in str(p) and "gmail-intel" not in str(p))
    else:
        paths = iterate_pursuits(data_root)

    for path in paths:
        try:
            fm, _, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            continue
        stage = fm.stage
        # Skip closed-lost — they don't count for quota either way
        if stage == Stage.CLOSED_LOST:
            continue
        raw_amount = effective_net_consulting_acv(
            fm.sf_contract_type,
            fm.sf_consulting_acv,
            fm.sf_acv,
            fm.sf_arr,
        )
        result.append({"stage": stage, "sf_amount": raw_amount, "sf_probability": fm.sf_probability, "name": path.stem})
    return result


def _account_search_term(slug: str, info: dict[str, object]) -> str:
    """Return the SOSL search term for an account: first keyword, else humanized slug.

    implementation note: The first configured keyword is the most specific searchable name
    (e.g. ``"Globex Corp"`` for slug ``"globex"``). Falls back to a humanized
    slug when no keywords are configured.
    """
    keywords = info.get("keywords")
    if isinstance(keywords, list) and keywords:
        first = keywords[0]
        if isinstance(first, str) and first.strip():
            return first
    return slug.replace("-", " ").replace("_", " ").title()


def _load_account_names(data_root: Path) -> list[str]:
    """Return SOSL search terms for accounts that have ``sf_territory`` set.

    implementation note: Accounts without ``sf_territory`` are not part of the operator's
    patch and are skipped. Returns ``[]`` when accounts.yaml is missing, malformed,
    or has no qualifying accounts.
    """
    accounts_path = data_root / "config" / "accounts.yaml"
    try:
        with accounts_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    accounts = data.get("accounts", {})
    if not isinstance(accounts, dict):
        return []
    names: list[str] = []
    for slug, info in accounts.items():
        if not isinstance(info, dict) or not info.get("sf_territory"):
            continue
        names.append(_account_search_term(str(slug), info))
    return names


def fetch_sf_closed_won(data_root: Path) -> float:
    """Pull live, territory-scoped closed-won consulting ACV from Salesforce.

    implementation note: The default pursuit-file closed-won sum is scoped to *configured*
    pursuits, which is not comparable to a full-book quota target. This pulls the
    real closed-won book for the operator's territories (fiscal-year-to-date).

    Lazy resolution: any patch account that has ``sf_territory`` but no
    ``sf_territory_id`` yet is resolved against live SF and written back to
    accounts.yaml, with a ``✓`` line printed per resolved account. The operator
    never looks up a Territory2Id by hand.

    Args:
        data_root: Workspace root (used to load patch account search terms).

    Returns:
        Sum of ``Consulting_Total_USD__c`` for closed-won FY opps in the
        operator's territories.

    Raises:
        ConfigError: if no territory IDs can be resolved after the lazy-resolution attempt.
        SFAuthError: if no SF session is configured or Salesforce returns HTTP 401.
        SFAPIError:  on other Salesforce API errors.
    """
    from fieldkit.config import (
        get_sf_rest_base_url,
        get_sf_session_id,
        get_sf_territory_ids_from_accounts,
    )
    from fieldkit.sf.client import SFAuthError, SFDirectClient
    from fieldkit.sf.territory import fetch_closed_won_by_territory

    sid = get_sf_session_id()
    if not sid:
        raise SFAuthError("No Salesforce session. Run: fieldkit auth sf")
    base_url = get_sf_rest_base_url()

    _resolve_missing_territory_ids(sid, base_url)

    territory_ids = get_sf_territory_ids_from_accounts()
    if not territory_ids:
        raise ConfigError(
            "No sf_territory_id values found in accounts.yaml. Ensure each account with "
            "sf_territory has at least one Salesforce opportunity so the ID can be resolved "
            "automatically, or add sf_territory_id manually to each account in accounts.yaml."
        )

    account_names = _load_account_names(data_root)
    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        return fetch_closed_won_by_territory(client, account_names, territory_ids)


def _configured_gsg_id(info: dict[str, object]) -> str | None:
    """Return a normalized account identity, or None when it is absent."""
    raw_gsg_id = info.get("sf_gsg_id")
    if not isinstance(raw_gsg_id, str):
        return None
    return raw_gsg_id.strip() or None


def _resolve_missing_territory_ids(sid: str, base_url: str) -> None:
    """Lazily resolve + persist ``sf_territory_id`` for patch accounts missing one.

    implementation note: A patch account has an ``sf_territory`` DeveloperName but no resolved
    ``sf_territory_id`` until its first ``--source sf`` run. This resolves each
    such account against live SF, writes the ID back to accounts.yaml, and prints
    a ``✓`` line (to stderr, so ``--json`` stdout stays clean). No-ops when every
    patch account is already resolved.
    """
    from fieldkit.config import get_accounts_config, set_sf_territory_id_for_account
    from fieldkit.sf.client import SFDirectClient
    from fieldkit.sf.territory import TerritoryResolutionRequest, resolve_territory_ids

    accts_raw = get_accounts_config().get("accounts", {})
    needs_resolution: dict[str, TerritoryResolutionRequest] = {}
    if isinstance(accts_raw, dict):
        for slug, info in accts_raw.items():
            if not isinstance(info, dict):
                continue
            dev_name = info.get("sf_territory")
            if dev_name and not info.get("sf_territory_id"):
                needs_resolution[str(slug)] = TerritoryResolutionRequest(
                    search_term=_account_search_term(str(slug), info),
                    expected_developer_name=str(dev_name),
                    gsg_id=_configured_gsg_id(info),
                )

    if not needs_resolution:
        return

    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        resolved = resolve_territory_ids(client, needs_resolution)
    for slug, tid in resolved.items():
        set_sf_territory_id_for_account(slug, tid)
        # Diagnostic to stderr so --json stdout stays clean machine output.
        click.echo(
            f"  ✓ Resolved sf_territory_id for {slug} ({needs_resolution[slug].expected_developer_name}): {tid}",
            err=True,
        )
    for slug in set(needs_resolution) - set(resolved):
        log.warning(
            "Could not resolve Territory2Id for %s (%s) — no matching opp found",
            slug,
            needs_resolution[slug].expected_developer_name,
        )
