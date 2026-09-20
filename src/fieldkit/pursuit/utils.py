"""Shared pursuit utility functions used by morning_brief and pipeline_review.

Provides helpers for iterating pursuit files, calculating date deltas,
extracting champion names, reading account configuration, and normalizing
monetary field values.
"""

import re
from collections.abc import Generator
from datetime import date
from functools import cache
from pathlib import Path

import yaml

_CHAMPION_SEP_RE = re.compile(r"[,(;]")

# Matches characters to strip from monetary strings: $, commas, whitespace.
_MONETARY_STRIP_RE = re.compile(r"[$,\s]")


def _parse_monetary(value: str | float | int | None) -> float | str | None:
    """Normalize a monetary field value to float where possible.

    Accepts the range of formats found in pursuit frontmatter:
    - None or empty string → None
    - Numeric (int/float) → float
    - Dollar-string like "$500,000" or "500,000.00" → float
    - Non-numeric strings (e.g. placeholders, free text) → returned as-is

    The lenient fallback (returning the original string) preserves backward
    compatibility with pursuit files that store non-monetary text in these
    fields. Callers that need a guaranteed float should check the return type.

    This function is intentionally placed in lib/pursuit.py (not lib/models.py)
    so it can be imported by both the Pydantic model validator and any display
    layer that needs to format monetary values.

    Args:
        value: Raw monetary value from YAML frontmatter.

    Returns:
        Normalized float, None if the value is absent/empty, or the original
        string if it cannot be parsed as a number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # String path: strip $, commas, whitespace then attempt conversion
    cleaned = _MONETARY_STRIP_RE.sub("", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        # Non-numeric string (e.g. placeholder, free text) — return as-is.
        # Chosen over raising so that existing pursuit files with non-standard
        # values continue to load without validation errors.
        return value


def iterate_pursuits(data_root: Path) -> Generator[Path, None, None]:
    """Yield each non-template, non-gmail-intel pursuit file path.

    Yields paths under data_root/accounts/*/pursuits/*.md, skipping
    any path containing '.template' or 'gmail-intel'.
    """
    for path in sorted(data_root.glob("accounts/*/pursuits/*.md")):
        if ".template" in str(path) or "gmail-intel" in str(path):
            continue
        yield path


def calculate_days_since(date_str: str) -> int:
    """Parse a YYYY-MM-DD date string and return (today - date).days.

    Returns -1 if the string is missing, empty, or unparseable.
    """
    if not date_str:
        return -1
    try:
        parsed = date.fromisoformat(str(date_str))
        return (date.today() - parsed).days
    except (ValueError, TypeError):
        return -1


@cache
def extract_champion_name(account_dir: Path) -> str:
    """Read account.md and extract the first champion's given name.

    Looks for a line starting with '**Champion:**' and returns the first word
    before any comma, semicolon, or parenthesis. Returns '' if not found.
    """
    acct_file = account_dir / "account.md"
    if not acct_file.exists():
        return ""
    for line in acct_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("**Champion:**"):
            name_part = line.removeprefix("**Champion:**").strip()
            first = _CHAMPION_SEP_RE.split(name_part)[0].strip()
            return first.split()[0] if first else ""
    return ""


@cache
def read_accounts_config(data_root: Path) -> dict[str, object]:
    """Read config/accounts.yaml and return the parsed dict.

    Raises FileNotFoundError if the config file is missing.
    Raises yaml.YAMLError if the file contains invalid YAML.
    """
    config_file = data_root / "config" / "accounts.yaml"
    with config_file.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def clear_pursuit_caches() -> None:
    """Clear cached pursuit values. Call in test fixtures for isolation."""
    extract_champion_name.cache_clear()
    read_accounts_config.cache_clear()
