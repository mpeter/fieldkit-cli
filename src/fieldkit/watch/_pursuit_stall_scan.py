"""Frontmatter and transition-date handling for the pursuit-stall watcher."""

import datetime
import logging
from pathlib import Path
from typing import Any

import click
import yaml

from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit import extract_champion_name
from fieldkit.pursuit.io import extract_frontmatter_text
from fieldkit.pursuit.qualification import native_qualification_status
from fieldkit.pursuit.stages import TERMINAL_STAGES
from fieldkit.watch import _pursuit_stall_state as stall_state

log = logging.getLogger(__name__)

_DEFAULT_STALL_DAYS = 14


def parse_frontmatter(text: str, path: Path | None = None) -> dict[str, Any]:
    """Extract parseable YAML frontmatter, reporting malformed source files."""
    frontmatter_text = extract_frontmatter_text(text)
    if not frontmatter_text:
        return {}
    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        file_ref = str(path) if path else "<unknown>"
        click.echo(
            f"\nPARSE ERROR — malformed frontmatter in: {file_ref}\n"
            f"  {exc}\n"
            "  Hint: run 'fieldkit pursuit audit' to validate all pursuit files.\n",
            err=True,
        )
        log.warning("YAML parse error in %s: %s", file_ref, exc, exc_info=False)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_transition_entry(entry: object) -> tuple[str, str, datetime.date] | None:
    """Normalize a transition-history entry to its stages and date."""
    if not isinstance(entry, dict):
        return None
    date = stall_state.coerce_date(entry.get("date"))
    if date is None:
        date = stall_state.coerce_date(entry.get("last-transition"))
    if date is None:
        return None
    if "from" in entry or "to" in entry:
        return str(entry.get("from", "")).strip(), str(entry.get("to", "")).strip(), date
    return "", str(entry.get("stage", "")).strip(), date


def find_last_transition_date(frontmatter: dict[str, Any]) -> datetime.date | None:
    """Return the canonical or newest recorded transition date."""
    date = stall_state.coerce_date(frontmatter.get("last-transition"))
    if date is not None:
        return date
    history = frontmatter.get("transition-history")
    if not isinstance(history, list):
        return None
    dates = [
        normalized[2]
        for entry in history
        if isinstance(entry, dict)
        if (normalized := normalize_transition_entry(entry))
    ]
    return max(dates) if dates else None


def scan_pursuit_file(
    path: Path,
    *,
    threshold_days: int,
    today: datetime.date,
) -> dict[str, Any] | None:
    """Parse one pursuit file and return its stall result, if scanable."""
    result, _reason, _intentional = scan_pursuit_file_details(path, threshold_days=threshold_days, today=today)
    return result


def scan_pursuit_file_details(
    path: Path,
    *,
    threshold_days: int,
    today: datetime.date,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Scan one file, retaining the skip reason for watcher observability."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc, exc_info=True)
        return None, f"cannot read file: {exc}", False

    frontmatter = parse_frontmatter(text, path=path)
    if not frontmatter:
        log.warning("pursuit-stalls: skipping %s — YAML parse error or missing frontmatter", path)
        return None, "missing or unparseable frontmatter", False

    stage = str(frontmatter.get("stage", "")).strip().lower()
    if stage in TERMINAL_STAGES:
        log.debug("Skipping terminal stage %r in %s", stage, path)
        return None, f"terminal stage: {stage}", True

    last_transition = find_last_transition_date(frontmatter)
    if last_transition is None:
        log.warning(
            "No last-transition date found in %s — skipping (add last-transition to frontmatter)",
            path,
        )
        return None, "missing last-transition date", False

    days_since = (today - last_transition).days
    try:
        relative_path = path.relative_to(get_fieldkit_home())
    except ValueError:
        relative_path = path

    parts = path.parts
    pursuits_index = next((index for index, part in enumerate(parts) if part == "pursuits"), None)
    account_key = parts[pursuits_index - 1] if pursuits_index is not None and pursuits_index >= 1 else "unknown"
    account_dir = path.parent.parent
    return (
        {
            "pursuit": path.stem,
            "path": str(relative_path),
            "account": account_key,
            "stage": stage,
            "last_transition_date": last_transition.isoformat(),
            "days_since_transition": days_since,
            "is_stalled": days_since > threshold_days,
            "threshold_days": threshold_days,
            "champion": extract_champion_name(account_dir),
            "sf_next_steps": str(frontmatter.get("sf_next_steps", "")).strip() or "(none)",
            "native_qualification": native_qualification_status(
                frontmatter["sf_opportunity_id"] if isinstance(frontmatter.get("sf_opportunity_id"), str) else None
            ),
        },
        None,
        False,
    )


def collect_pursuit_files(
    *,
    accounts_config: dict[str, Any],
    account_filter: str | None,
) -> list[tuple[Path, int]]:
    """Return the configured pursuit files and their effective thresholds."""
    return collect_pursuit_files_details(accounts_config=accounts_config, account_filter=account_filter)[0]


def collect_pursuit_files_details(
    *,
    accounts_config: dict[str, Any],
    account_filter: str | None,
) -> tuple[list[tuple[Path, int]], int, int, set[str]]:
    """Collect files with intentional-exclusion and failed-account counts."""
    accounts = accounts_config.get("accounts", {})
    if not isinstance(accounts, dict):
        return [], 0, 0, set()

    results: list[tuple[Path, int]] = []
    intentional_exclusions = data_failures = 0
    failed_accounts: set[str] = set()
    for account_key, account_config in accounts.items():
        if account_filter and account_key != account_filter:
            log.info("pursuit-stalls: excluding account %r — outside account filter", account_key)
            continue
        if not isinstance(account_config, dict):
            log.warning("pursuit-stalls: cannot scan account %r — config is not a mapping", account_key)
            data_failures += 1
            failed_accounts.add(account_key)
            continue
        if account_config.get("internal"):
            log.info("pursuit-stalls: excluding account %r — internal account", account_key)
            intentional_exclusions += 1
            continue

        try:
            threshold = int(account_config.get("stall_threshold_days", _DEFAULT_STALL_DAYS))
        except (TypeError, ValueError):
            threshold = _DEFAULT_STALL_DAYS

        pursuit_dir = get_fieldkit_home() / account_config.get("pursuit_dir", f"accounts/{account_key}/pursuits")
        if not pursuit_dir.is_dir():
            log.warning(
                "pursuit-stalls: cannot scan account %r — pursuit directory not found: %s", account_key, pursuit_dir
            )
            data_failures += 1
            failed_accounts.add(account_key)
            continue

        for pursuit_file in sorted(pursuit_dir.glob("*.md")):
            if pursuit_file.stem in ("template", "gmail-intel", "index", "README"):
                log.info("pursuit-stalls: excluding %s — template or index file", pursuit_file)
                intentional_exclusions += 1
                continue
            results.append((pursuit_file, threshold))

    return results, intentional_exclusions, data_failures, failed_accounts
