"""Classify meeting tasks from rendered notes before publishing file effects."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from fieldkit.pursuit.io import extract_frontmatter_text
from fieldkit.tasks.classifier import ClassifiedItem

_DISPLAY_OVERRIDES: dict[str, str] = {
    "globalpay": "GlobalPay",
    "acme-corp": "Acme-Corp",  # pii-guard: ignore
    "rhoai": "RHOAI",
    "aep": "AEP",
    "eda": "EDA",
    "aap": "AAP",
    "ocp": "OCP",
    "eap": "EAP",
    "hcs": "HCS",
    "sow": "SOW",
    "mfa": "MFA",
    "ads": "ADS",
    "leapp": "LEAPP",
    "rhocp": "RHOCP",
}


def _display_slug(slug: str) -> str:
    """Convert a slug to a display name using known abbreviation overrides."""
    parts = slug.replace("-", " ").split()
    return " ".join(_DISPLAY_OVERRIDES.get(part.lower(), part.title()) for part in parts)


def build_pursuit_label(account: str, pursuits: Sequence[str]) -> str:
    """Build the pursuit label for TASKS.md tagging."""
    account_display = _DISPLAY_OVERRIDES.get(account.lower(), account.replace("-", " ").title())
    return f"{account_display} / {_display_slug(pursuits[0])}" if pursuits else account_display


def _attendee_names(frontmatter: Mapping[str, object], key: str) -> list[str]:
    """Read an optional attendee list without coercing malformed ownership data."""
    value = frontmatter.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"Invalid {key}: expected a list of names")
    names: list[str] = []
    for name in value:
        if not isinstance(name, str):
            raise ValueError(f"Invalid {key}: expected string names")
        if not name.strip():
            raise ValueError(f"Invalid {key}: expected nonblank names")
        names.append(name.strip())
    return names


def parse_meeting_frontmatter(note_content: str) -> dict[str, object]:
    """Validate a generated note's mapping without reflecting its content."""
    fm_text = extract_frontmatter_text(note_content)
    if not fm_text:
        raise ValueError("Invalid meeting frontmatter")
    loader = YAML(typ="safe", pure=True)
    loader.version = (1, 1)
    loader.allow_duplicate_keys = False
    try:
        fm = loader.load(fm_text)
    except (YAMLError, RecursionError):
        raise ValueError("Invalid meeting frontmatter") from None
    if not isinstance(fm, dict):
        raise ValueError("Invalid meeting frontmatter")
    return fm


def _attendee_names_from_note(note_content: str, known_internal_names: list[str]) -> tuple[list[str], list[str]]:
    """Read internal-team and stakeholder names from an in-memory note."""
    fm = parse_meeting_frontmatter(note_content)
    vault_internal = _attendee_names(fm, "attendees_internal")
    internal_team_names = list(dict.fromkeys(known_internal_names + vault_internal))
    vault_external = [
        re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() for name in _attendee_names(fm, "attendees_external")
    ]
    if any(not name for name in vault_external):
        raise ValueError("Invalid attendees_external: expected nonblank names")
    internal_lower = {name.lower() for name in internal_team_names}
    stakeholder_names = [
        name
        for name in vault_external
        if not any(internal in name.lower() or name.lower() in internal for internal in internal_lower)
    ]
    return internal_team_names, stakeholder_names


def classify_meeting_tasks(
    *,
    data_root: Path,
    action_items: Sequence[str],
    pursuits: Sequence[str],
    account: str,
    note_content: str,
) -> tuple[ClassifiedItem, ...]:
    """Classify exact task decisions before any output file is written."""
    if not action_items:
        return ()

    from fieldkit.config import get_accounts_config, get_user_email, get_user_name
    from fieldkit.tasks.classifier import classify_action_items

    user_name = get_user_name()
    user_email = get_user_email() or ""
    accounts = get_accounts_config(workspace_root=data_root, strict=True)
    account_map = accounts.get("accounts", {})
    if not isinstance(account_map, dict):
        raise ValueError("Invalid account task configuration")
    account_info = account_map.get(account, {})
    if not isinstance(account_info, dict):
        raise ValueError("Invalid account task configuration")
    known_internal_names = _attendee_names(account_info, "internal_team_display_names")
    internal_team_names, stakeholder_names = _attendee_names_from_note(note_content, known_internal_names)
    classified = classify_action_items(
        list(action_items),
        user_name=user_name,
        user_email=user_email,
        stakeholder_names=stakeholder_names,
        internal_team_names=internal_team_names,
        pursuit_label=build_pursuit_label(account, pursuits),
    )
    return tuple(classified)
