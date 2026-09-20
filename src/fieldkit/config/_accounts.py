"""Private accounts.yaml configuration accessors."""

import logging
from typing import Any

import yaml

from fieldkit.config import _loader
from fieldkit.config._paths import get_config_path
from fieldkit.util.atomic import atomic_yaml_write

logger = logging.getLogger(__name__)


@_loader._config_cache
def _load_accounts_yaml() -> dict[str, object]:
    """Load accounts.yaml, returning an empty mapping for unreadable input."""
    try:
        path = get_config_path("accounts.yaml")
    except _loader.ConfigError:
        return {}
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        logger.warning("accounts.yaml: could not read %s: %s", path, exc)
        return {}
    except yaml.YAMLError as exc:
        logger.warning("accounts.yaml: invalid YAML in %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("accounts.yaml: expected a YAML mapping in %s, got %s — ignoring", path, type(data).__name__)
        return {}
    return data


def get_accounts_config() -> dict[str, Any]:
    """Return the parsed accounts configuration, or an empty mapping."""
    return _load_accounts_yaml()


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


def get_sf_territory_ids_from_accounts() -> list[str]:
    """Return distinct resolved Salesforce Territory2 identifiers."""
    raw = _load_accounts_yaml().get("accounts", {})
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


def set_sf_territory_id_for_account(account_slug: str, territory_id: str) -> None:
    """Atomically write a resolved Salesforce territory ID for one account."""
    try:
        path = get_config_path("accounts.yaml")
    except _loader.ConfigError:
        return
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(data, dict):
        return
    accounts = data.get("accounts", {})
    if not isinstance(accounts, dict) or account_slug not in accounts:
        return
    account = accounts[account_slug]
    if not isinstance(account, dict):
        return
    account["sf_territory_id"] = territory_id
    atomic_yaml_write(path, data)
    _load_accounts_yaml.cache_clear()  # type: ignore[attr-defined]


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
