"""Private accounts.yaml configuration accessors."""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fieldkit.config import _loader
from fieldkit.config._paths import get_config_path
from fieldkit.util.atomic import atomic_yaml_write, exclusive_path_lock

logger = logging.getLogger(__name__)
_TERRITORY_WRITE_LOCK_TIMEOUT_SECONDS = 5


def _accounts_path(workspace_root: Path | None = None) -> Path:
    """Resolve one account file while requiring a stable parent namespace.

    Explicit workspace aliases are allowed; child redirects are not. Absent
    workspaces may be inspected without creation using their existing ancestor.
    This does not protect against hostile same-user namespace replacement.
    """
    if workspace_root is None:
        return get_config_path("accounts.yaml")
    try:
        return get_config_path("accounts.yaml", workspace_root=workspace_root)
    except _loader.ConfigError:
        raise _loader.ConfigError("Cannot locate confined accounts.yaml") from None


def read_accounts_mapping_for_update(path: Path) -> dict[str, object]:
    """Validate account-index structure before an initialization merge."""
    data = _loader.read_config_mapping_for_update(path)
    accounts = data.get("accounts", {})
    if not isinstance(accounts, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in accounts.items()
    ):
        raise _loader.ConfigError("Existing account configuration must contain an accounts mapping")
    domains = data.get("internal_domains", [])
    if not isinstance(domains, list) or any(not isinstance(value, str) for value in domains):
        raise _loader.ConfigError("Existing account configuration must contain an internal_domains list")
    return data


def _read_accounts_yaml(workspace_root: Path | None = None) -> dict[str, object]:
    """Read current accounts configuration, distinguishing absence from failure."""
    try:
        path = _accounts_path(workspace_root)
    except _loader.ConfigError:
        raise _loader.ConfigError("Cannot locate accounts.yaml") from None
    try:
        return read_accounts_mapping_for_update(path)
    except _loader.ConfigError:
        raise _loader.ConfigError("Invalid or unreadable accounts.yaml") from None


@_loader._config_cache
def _load_accounts_yaml() -> dict[str, object]:
    """Load optional account settings with a logged empty-mapping fallback."""
    try:
        return _read_accounts_yaml()
    except _loader.ConfigError as exc:
        logger.warning("%s; ignoring optional account settings", exc)
        return {}


def get_accounts_config(*, strict: bool = False, workspace_root: Path | None = None) -> dict[str, Any]:
    """Read accounts settings; strict reads are fresh and propagate failures.

    An absent file means no overrides in either mode. Ownership-sensitive
    callers must request strict reads rather than cached optional fallbacks.
    An explicit workspace selects a fresh validated read, never a configured-root
    fallback, independently of the default optional cache.
    """
    return _read_accounts_yaml(workspace_root) if strict or workspace_root is not None else _load_accounts_yaml()


def get_account_names() -> list[str]:
    """Return configured account slugs."""
    accounts = _load_accounts_yaml().get("accounts", {})
    return list(accounts.keys()) if isinstance(accounts, dict) else []


def get_gsg_id(account_slug: str) -> str | None:
    """Return one account's stripped nonempty sf_gsg_id."""
    accounts = _load_accounts_yaml().get("accounts", {})
    if not isinstance(accounts, dict):
        return None
    info = accounts.get(account_slug)
    if not isinstance(info, dict):
        return None
    value = info.get("sf_gsg_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def get_account_ids(account_slug: str) -> list[str]:
    """Return one account's unique nonblank sf_account_ids in configured order."""
    accounts = _load_accounts_yaml().get("accounts", {})
    if not isinstance(accounts, dict):
        return []
    info = accounts.get(account_slug)
    if not isinstance(info, dict):
        return []
    values = info.get("sf_account_ids")
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def get_internal_domains() -> list[str]:
    """Return configured internal email domains."""
    domains = _load_accounts_yaml().get("internal_domains", [])
    return list(domains) if isinstance(domains, list) else []


def get_salesforce_org_url() -> str:
    """Return the Salesforce organization URL from accounts.yaml."""
    salesforce = _load_accounts_yaml().get("salesforce", {})
    value = salesforce.get("org_url", "") if isinstance(salesforce, dict) else ""
    return str(value) if value else ""


def get_territory_account_map() -> dict[str, str]:
    """Return sf_territory-to-account-slug mappings."""
    raw = _load_accounts_yaml().get("accounts", {})
    accounts: dict[str, object] = raw if isinstance(raw, dict) else {}
    result: dict[str, str] = {}
    for name, info in accounts.items():
        if isinstance(info, dict):
            territory = info.get("sf_territory")
            if territory and isinstance(territory, str):
                result[territory] = str(name)
    return result


def get_sf_territory_ids_from_accounts(*, workspace_root: Path | None = None, strict: bool = False) -> list[str]:
    """Return distinct resolved Salesforce Territory2 identifiers."""
    raw = get_accounts_config(workspace_root=workspace_root, strict=strict).get("accounts", {})
    accounts: dict[str, object] = raw if isinstance(raw, dict) else {}
    seen: set[str] = set()
    result: list[str] = []
    for info in accounts.values():
        if not isinstance(info, dict):
            continue
        territory_id = info.get("sf_territory_id")
        if territory_id and isinstance(territory_id, str) and territory_id not in seen:
            seen.add(territory_id)
            result.append(territory_id)
    return result


def require_accounts_snapshot(expected_accounts: Mapping[str, object], *, workspace_root: Path) -> None:
    """Reject a changed account mapping across a caller's observation boundary.

    This validates accounts only, not unrelated top-level settings, and does not
    hold a lock across provider requests or serialize noncooperative writers.
    """
    current = get_accounts_config(strict=True, workspace_root=workspace_root).get("accounts", {})
    if not _same_account_snapshot(current, dict(expected_accounts)):
        raise _loader.ConfigError("Accounts in accounts.yaml changed during quota observation")


def set_sf_territory_id_for_account(
    account_slug: str,
    territory_id: str,
    *,
    workspace_root: Path | None = None,
    expected_account: Mapping[str, object] | None = None,
) -> None:
    """Publish one fresh account update under a bounded cooperative lock.

    Other writers must use the same lock to avoid lost updates. A supplied
    snapshot rejects stale resolution; noncooperative writers are not serialized.
    """
    if not isinstance(territory_id, str) or not territory_id.strip():
        raise _loader.ConfigError("Invalid territory identity for accounts.yaml")
    if workspace_root is not None and not workspace_root.is_dir():
        raise _loader.ConfigError("Workspace for accounts.yaml must already exist")
    path = _accounts_path(workspace_root)
    _territory_account(read_accounts_mapping_for_update(path), account_slug)
    with exclusive_path_lock(path, timeout_seconds=_TERRITORY_WRITE_LOCK_TIMEOUT_SECONDS):
        data = read_accounts_mapping_for_update(path)
        account = _territory_account(data, account_slug)
        if expected_account is not None and not _same_account_snapshot(account, dict(expected_account)):
            raise _loader.ConfigError("Account in accounts.yaml changed during territory resolution")
        account["sf_territory_id"] = territory_id
        if _accounts_path(workspace_root) != path:
            raise _loader.ConfigError("Location of accounts.yaml changed during territory resolution")
        atomic_yaml_write(path, data)
    _load_accounts_yaml.cache_clear()  # type: ignore[attr-defined]


def _territory_account(data: dict[str, object], account_slug: str) -> dict[str, Any]:
    accounts = data.get("accounts", {})
    account = accounts.get(account_slug) if isinstance(accounts, dict) else None
    if not isinstance(account, dict):
        raise _loader.ConfigError("Account to update is absent from accounts.yaml")
    return account


def _same_account_snapshot(current: object, expected: object, seen: set[tuple[int, int]] | None = None) -> bool:
    """Keep YAML scalar type identity: true must not authenticate numeric one."""
    if type(current) is not type(expected):
        return False
    if seen is None:
        seen = set()
    if isinstance(current, (dict, list)):
        pair = (id(current), id(expected))
        if pair in seen:
            return True
        seen.add(pair)
    if isinstance(current, dict) and isinstance(expected, dict):
        return {(type(key), key) for key in current} == {(type(key), key) for key in expected} and all(
            _same_account_snapshot(value, expected[key], seen) for key, value in current.items()
        )
    if isinstance(current, list) and isinstance(expected, list):
        return len(current) == len(expected) and all(
            _same_account_snapshot(left, right, seen) for left, right in zip(current, expected, strict=True)
        )
    if isinstance(current, set) and isinstance(expected, set):
        return {(type(value), value) for value in current} == {(type(value), value) for value in expected}
    return current == expected


def _add_domain_account_mapping(
    result: dict[str, str], ambiguous_domains: set[str], normalized_domain: str, account_slug: str
) -> None:
    """Add one domain claim, permanently omitting cross-account conflicts."""
    if normalized_domain in ambiguous_domains:
        return
    mapped_slug = result.get(normalized_domain)
    if mapped_slug is None or mapped_slug == account_slug:
        result[normalized_domain] = account_slug
        return
    result.pop(normalized_domain)
    ambiguous_domains.add(normalized_domain)
    first_slug, second_slug = sorted((mapped_slug, account_slug))
    logger.warning(
        "accounts.yaml: domain %s is claimed by accounts %s and %s; omitting ambiguous mapping",
        normalized_domain,
        first_slug,
        second_slug,
    )


def build_domain_account_map() -> dict[str, str]:
    """Return unambiguous email-domain-to-account-slug mappings."""
    accounts = _load_accounts_yaml().get("accounts", {})
    if not isinstance(accounts, dict):
        return {}
    result: dict[str, str] = {}
    ambiguous_domains: set[str] = set()
    for slug, info in accounts.items():
        if not isinstance(info, dict):
            continue
        domains = info.get("domains", [])
        if not isinstance(domains, list):
            continue
        account_slug = str(slug)
        for domain in domains:
            if isinstance(domain, str) and domain:
                _add_domain_account_mapping(result, ambiguous_domains, domain.lower(), account_slug)
    return result
