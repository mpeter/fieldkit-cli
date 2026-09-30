"""Salesforce territory-scoped closed-won quota collection."""

import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from fieldkit.config import ConfigError, get_accounts_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SFQuotaResult:
    amount: float
    resolved_account_slugs: tuple[str, ...]


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
    patch and are skipped. An absent file or no qualifying accounts returns
    ``[]``. Present invalid configuration raises ConfigError.
    """
    return _account_names(_quota_accounts(data_root))


def _account_names(accounts: dict[str, dict[str, object]]) -> list[str]:
    """Project search terms from the same validated scope used for IDs."""
    names: list[str] = []
    for slug, info in accounts.items():
        if not info.get("sf_territory"):
            continue
        names.append(_account_search_term(slug, info))
    return names


def _quota_accounts(data_root: Path) -> dict[str, dict[str, object]]:
    """Validate selected ownership identities before authentication or queries."""
    from fieldkit.sf.territory import is_territory_id

    raw = get_accounts_config(strict=True, workspace_root=data_root).get("accounts", {})
    if not isinstance(raw, dict):
        raise ConfigError("Invalid accounts mapping in accounts.yaml")
    accounts: dict[str, dict[str, object]] = {}
    for slug, info in raw.items():
        if not isinstance(slug, str) or not slug.strip() or not isinstance(info, dict):
            raise ConfigError("Invalid account entry in accounts.yaml")
        developer_name = info.get("sf_territory")
        if developer_name not in (None, "") and (not isinstance(developer_name, str) or not developer_name.strip()):
            raise ConfigError("Invalid sf_territory in accounts.yaml")
        territory_id = info.get("sf_territory_id")
        if territory_id not in (None, "") and (not isinstance(territory_id, str) or not is_territory_id(territory_id)):
            raise ConfigError("Invalid sf_territory_id in accounts.yaml")
        _configured_gsg_id(info)
        accounts[slug] = info
    return accounts


def fetch_sf_closed_won(data_root: Path) -> SFQuotaResult:
    """Pull live, territory-scoped closed-won consulting ACV from Salesforce.

    implementation note: The default pursuit-file closed-won sum is scoped to *configured*
    pursuits, which is not comparable to a full-book quota target. This pulls the
    real closed-won book for the operator's territories (fiscal-year-to-date).

    Lazy resolution: any patch account that has ``sf_territory`` but no
    ``sf_territory_id`` yet is resolved against live SF and written back to
    accounts.yaml, with resolved slugs returned to the CLI adapter. The operator
    never looks up a Territory2Id by hand.

    Args:
        data_root: Workspace root for account scope, territory reads and writes.

    Returns:
        Closed-won amount and slugs whose territory IDs were resolved.

    Raises:
        ConfigError: if no territory IDs can be resolved after the lazy-resolution attempt.
        SFAuthError: if no SF session is configured or Salesforce returns HTTP 401.
        SFAPIError:  on other Salesforce API errors.
    """
    from fieldkit.config import (
        get_sf_rest_base_url,
        get_sf_session_id,
        require_accounts_snapshot,
    )
    from fieldkit.sf.client import SFDirectClient
    from fieldkit.sf.errors import SFAuthError
    from fieldkit.sf.territory import fetch_closed_won_by_territory

    accounts = deepcopy(_quota_accounts(data_root))
    initial_accounts = deepcopy(accounts)
    account_names = _account_names(accounts)
    if not account_names:
        raise ConfigError("No sf_territory or sf_territory_id scope configured in accounts.yaml")

    sid = get_sf_session_id()
    if not sid:
        raise SFAuthError("No Salesforce session. Run: fieldkit auth sf")
    base_url = get_sf_rest_base_url()

    require_accounts_snapshot(accounts, workspace_root=data_root)
    accounts = _resolve_missing_territory_ids(sid, base_url, data_root=data_root, accounts_snapshot=accounts)

    territory_ids = list(
        dict.fromkeys(
            value for info in accounts.values() if isinstance(value := info.get("sf_territory_id"), str) and value
        )
    )
    if not territory_ids:
        raise ConfigError(
            "No sf_territory_id values found in accounts.yaml. Ensure each account with "
            "sf_territory has at least one Salesforce opportunity so the ID can be resolved "
            "automatically, or add sf_territory_id manually to each account in accounts.yaml."
        )

    account_names = _account_names(accounts)
    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        require_accounts_snapshot(accounts, workspace_root=data_root)
        result = fetch_closed_won_by_territory(client, account_names, territory_ids)
    require_accounts_snapshot(accounts, workspace_root=data_root)
    resolved_slugs = tuple(
        slug
        for slug, info in accounts.items()
        if info.get("sf_territory_id") and not initial_accounts[slug].get("sf_territory_id")
    )
    return SFQuotaResult(amount=result, resolved_account_slugs=resolved_slugs)


def _configured_gsg_id(info: dict[str, object]) -> str | None:
    """Return a normalized account identity, or None when it is absent."""
    from fieldkit.sf.territory import is_gsg_id

    raw_gsg_id = info.get("sf_gsg_id")
    if raw_gsg_id is None:
        return None
    if not isinstance(raw_gsg_id, str):
        raise ConfigError("Invalid sf_gsg_id in accounts.yaml")
    normalized = raw_gsg_id.strip()
    if normalized and not is_gsg_id(normalized):
        raise ConfigError("Invalid sf_gsg_id in accounts.yaml")
    return normalized or None


def _resolve_missing_territory_ids(
    sid: str, base_url: str, *, data_root: Path, accounts_snapshot: dict[str, dict[str, object]]
) -> dict[str, dict[str, object]]:
    """Lazily resolve + persist ``sf_territory_id`` for patch accounts missing one.

    implementation note: A patch account has an ``sf_territory`` DeveloperName but no resolved
    ``sf_territory_id`` until its first ``--source sf`` run. This resolves each
    such account against live SF, writes the ID back to accounts.yaml, and returns the updated account snapshot. No-ops when every
    patch account is already resolved.
    """
    from fieldkit.config import require_accounts_snapshot, set_sf_territory_id_for_account
    from fieldkit.sf.client import SFDirectClient
    from fieldkit.sf.territory import TerritoryResolutionRequest, is_territory_id, resolve_territory_ids

    accts_raw = deepcopy(accounts_snapshot)
    require_accounts_snapshot(accts_raw, workspace_root=data_root)
    needs_resolution: dict[str, TerritoryResolutionRequest] = {}
    snapshots: dict[str, dict[str, object]] = {}
    for slug, info in accts_raw.items():
        dev_name = info.get("sf_territory")
        if isinstance(dev_name, str) and dev_name and not info.get("sf_territory_id"):
            needs_resolution[slug] = TerritoryResolutionRequest(
                search_term=_account_search_term(slug, info),
                expected_developer_name=dev_name,
                gsg_id=_configured_gsg_id(info),
            )
            snapshots[slug] = deepcopy(info)

    if not needs_resolution:
        return accts_raw

    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        require_accounts_snapshot(accts_raw, workspace_root=data_root)
        resolved = resolve_territory_ids(client, needs_resolution)
    require_accounts_snapshot(accts_raw, workspace_root=data_root)
    for slug, tid in resolved.items():
        if slug not in needs_resolution:
            raise ConfigError("Unexpected resolved account for accounts.yaml")
        if not isinstance(tid, str) or not is_territory_id(tid):
            raise ConfigError("Invalid resolved territory identity for accounts.yaml")
        set_sf_territory_id_for_account(slug, tid, workspace_root=data_root, expected_account=snapshots[slug])
        accts_raw[slug]["sf_territory_id"] = tid
        require_accounts_snapshot(accts_raw, workspace_root=data_root)
    unresolved_count = len(set(needs_resolution) - set(resolved))
    if unresolved_count:
        log.warning("Could not resolve territory IDs for %d configured account(s)", unresolved_count)
    return accts_raw
