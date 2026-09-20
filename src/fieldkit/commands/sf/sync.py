"""File I/O helper for Salesforce sync.

Subcommands:
    list-pursuits <account|--all>   Print opp_id<TAB>file_path for pursuits with sf_opportunity_id.
    write-opp <opp-id> <file> <json>  Write cache JSON + update pursuit frontmatter.
    write-account <name> <json>     Write cache JSON + update account.md frontmatter.
    match-pursuit <dir> <opp-id>    Print the pursuit file matching an opp ID, or nothing.

CLI usage:
    python -m sf_pipeline sync list-pursuits --all
    python -m sf_pipeline sync write-opp 006... accounts/global-pay/pursuits/deal.md '{"status":"ok",...}'
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.commands.sf.frontmatter import _run_sf_mode
from fieldkit.commands.sf.reconcile import _run_reconcile as _reconcile_with_path
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.io import _split_frontmatter, load_pursuit
from fieldkit.sf.opportunities import is_opportunity_id

LOG_PREFIX = "[sf-sync]"


def _load_known_accounts() -> tuple[str, ...]:
    """Load account names from project tree, then config, then hardcoded fallback.

    When SF_PIPELINE_ROOT is set (tests, CI), prefer the accounts/ dir there
    over the global config — the config may reference a different set of accounts.
    """
    # 1. Scan accounts/ directory under project root (authoritative when present)
    accts_dir = _project_root() / "accounts"
    if accts_dir.is_dir():
        found = tuple(d.name for d in sorted(accts_dir.iterdir()) if d.is_dir() and not d.name.startswith("."))
        if found:
            return found
    # 2. Fall back to config/accounts.yaml
    try:
        from fieldkit.config import get_account_names

        names = get_account_names()
        if names:
            return tuple(names)
    except (ImportError, OSError, ValueError):
        pass  # optional dep — fall back to hardcoded list
    return ("global-pay", "acme-bank", "shield-ins")


PLACEHOLDER_VALUES = {"tbd", "placeholder", "todo", "xxx", "none", "n/a", "na", "null", ""}


def _project_root() -> Path:
    # implementation note: FIELDKIT_SF_PIPELINE_ROOT is the primary name; SF_PIPELINE_ROOT is the legacy alias.
    override = os.environ.get("FIELDKIT_SF_PIPELINE_ROOT") or os.environ.get("SF_PIPELINE_ROOT")
    if override:
        return Path(override)
    return get_fieldkit_home()


def _known_accounts() -> tuple[str, ...]:
    return _load_known_accounts()


def _cache_dir() -> Path:
    return _project_root() / ".cache" / "salesforce"


def _pursuits_dir(account: str) -> Path:
    return _project_root() / "accounts" / account / "pursuits"


def _detect_comment_artifact(filepath: Path, fm_text: str) -> None:
    """Warn if sf_opportunity_id looks like a YAML comment artifact.

    YAML silently discards inline comments, so ``raw.get()`` returns None for
    lines like ``sf_opportunity_id: # was 006...``.  This helper scans the raw
    frontmatter text and emits an actionable warning when that pattern is found.

    Extracted from ``_extract_opp_id`` to reduce cyclomatic complexity (SOLID SRP).
    """
    for key in ("sf_opportunity_id", "sf-opportunity-id"):
        for line in fm_text.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}:"):
                remainder = stripped[len(f"{key}:") :].strip()
                if remainder.startswith("#"):
                    logging.warning(
                        "sf_opportunity_id in %s looks like a YAML comment artifact: %r"
                        " — the original ID was likely replaced by a comment."
                        " Correct the file to restore the opportunity ID.",
                        filepath,
                        remainder,
                    )


def _extract_opp_id(filepath: Path) -> str | None:
    """Extract sf_opportunity_id from a pursuit file's YAML frontmatter."""
    try:
        model, _, _ = load_pursuit(filepath)
        val = model.sf_opportunity_id
        if val:
            raw_val = str(val).strip().strip("\"'")
            if raw_val.lower() in PLACEHOLDER_VALUES:
                logging.warning(
                    "sf_opportunity_id in %s is a placeholder: %r — skipping sync",
                    filepath,
                    raw_val,
                )
                return None
            if not is_opportunity_id(raw_val):
                logging.warning(
                    "sf_opportunity_id in %s looks invalid: %r — expected 15- or 18-char "
                    "alphanumeric Salesforce ID — skipping sync",
                    filepath,
                    raw_val,
                )
                return None
            return raw_val
    except (OSError, ValueError) as exc:
        # load_pursuit raises ValueError for malformed frontmatter or missing
        # required fields (e.g. 'stage'). Fall back to raw YAML extraction so
        # that files with sf_opportunity_id but no stage still return the ID.
        logging.warning("load_pursuit failed for %s, using raw YAML fallback: %s", filepath, exc, exc_info=True)
    try:
        text = filepath.read_text(encoding="utf-8")
        fm_text, _ = _split_frontmatter(text)
        raw = yaml.safe_load(fm_text) or {}
        for key in ("sf_opportunity_id", "sf-opportunity-id"):
            val = raw.get(key)
            if val:
                extracted = str(val).strip().strip("\"'")
                if extracted.lower() in PLACEHOLDER_VALUES:
                    logging.warning(
                        "sf_opportunity_id in %s is a placeholder: %r — skipping sync",
                        filepath,
                        extracted,
                    )
                    return None
                # Warn if extracted value is not a valid opp ID (e.g. comment artifact).
                if not is_opportunity_id(extracted):
                    logging.warning(
                        "sf_opportunity_id in %s appears corrupt: %r — expected 15- or 18-char alphanumeric ID",
                        filepath,
                        extracted,
                    )
                    return None
                return extracted
        # Check raw text for comment-style corruption: `sf_opportunity_id: # was 006...`
        # Delegate to helper to keep this function's CC below the CRAP threshold.
        _detect_comment_artifact(filepath, fm_text)
        return None
    except (OSError, ValueError):
        return None


def _validate_opp_id(opp_id: str) -> bool:
    return is_opportunity_id(opp_id)


def _parse_json(json_string: str) -> dict[str, Any]:
    try:
        result: dict[str, Any] = json.loads(json_string)
        return result
    except json.JSONDecodeError as e:
        click.echo(f"{LOG_PREFIX} ERROR: Invalid JSON: {e}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None


# ── Subcommands ───────────────────────────────────────────────────────────────


def do_list_pursuits(target: str) -> None:
    """List pursuit files with their Salesforce opportunity IDs."""
    accounts = list(_known_accounts()) if target == "--all" else [target]

    for account in accounts:
        pdir = _pursuits_dir(account)
        if not pdir.is_dir():
            continue
        for pursuit_file in sorted(pdir.glob("*.md")):
            if pursuit_file.name in ("template.md", "gmail-intel.md"):
                continue
            opp_id = _extract_opp_id(pursuit_file)
            if opp_id and _validate_opp_id(opp_id):
                click.echo(f"{opp_id}\t{pursuit_file.relative_to(_project_root())}")


def do_write_opp(opp_id: str, pursuit_file: str, json_string: str) -> None:
    """Write extracted Salesforce opportunity data to cache and update pursuit frontmatter."""
    if not _validate_opp_id(opp_id):
        click.echo(
            f"{LOG_PREFIX} ERROR: sf_opportunity_id '{opp_id}' is invalid."
            " Expected format: 006… (15 or 18 alphanumeric characters).",
            err=True,
        )
        raise SystemExit(EXIT_DATA)

    pf = Path(pursuit_file)
    if not pf.is_file():
        click.echo(f"{LOG_PREFIX} ERROR: Pursuit file not found: {pursuit_file}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    data = _parse_json(json_string)
    if data.get("status") != "ok":
        click.echo(f"{LOG_PREFIX} ERROR: JSON status is not 'ok' for {opp_id}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    # Write cache
    cache_file = _cache_dir() / f"{opp_id}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("pulled_at", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    cache_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    click.echo(f"{LOG_PREFIX} Cache: {cache_file.relative_to(_project_root())}", err=True)

    # Update frontmatter
    _run_sf_mode(str(pf), json_string)
    click.echo(f"{LOG_PREFIX} Frontmatter: {pursuit_file}", err=True)

    # Reconcile key fields — call _reconcile_with_path directly to avoid sys.argv mutation.
    # historic regression fix: _run_reconcile now raises FileNotFoundError instead of sys.exit(1).
    # We catch both FileNotFoundError and any residual SystemExit (defensive) so that
    # a reconcile failure never kills the outer listview account loop.
    try:
        _reconcile_with_path(str(pf))
    except (SystemExit, FileNotFoundError, Exception):  # noqa: BLE001
        logging.warning("Reconcile failed for %s", pf, exc_info=True)

    # Post-write validation: warn if the file still fails model validation after write.
    # This is a 'fail fast' guard — it warns operators but does not block the sync.
    try:
        load_pursuit(pf)
    except ValueError as exc:
        logging.warning("Pursuit file fails validation after write: %s (%s)", pf, exc, exc_info=True)


def do_write_account(account_name: str, json_string: str) -> None:
    """Write extracted Salesforce account data to cache and update account frontmatter."""
    if account_name not in _known_accounts():
        click.echo(
            f"{LOG_PREFIX} ERROR: Unknown account '{account_name}' — must be {', '.join(_known_accounts())}",
            err=True,
        )
        raise SystemExit(EXIT_PARTIAL)

    account_file = _project_root() / "accounts" / account_name / "account.md"
    if not account_file.is_file():
        click.echo(f"{LOG_PREFIX} ERROR: Account file not found: {account_file}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    data = _parse_json(json_string)
    if data.get("status") != "ok":
        click.echo(f"{LOG_PREFIX} ERROR: JSON status is not 'ok' for {account_name}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    # Write cache
    cache_dir = _cache_dir() / "accounts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{account_name}.json"
    cache_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    click.echo(f"{LOG_PREFIX} Cache: {cache_file.relative_to(_project_root())}", err=True)

    # Update frontmatter
    _run_sf_mode(str(account_file), json_string)
    click.echo(f"{LOG_PREFIX} Frontmatter: {account_file.relative_to(_project_root())}", err=True)


def do_match_pursuit(pursuit_dir: str, opp_id: str) -> None:
    """Find which pursuit file in a directory matches a given opportunity ID."""
    pdir = Path(pursuit_dir)
    if not pdir.is_dir():
        return

    for md in sorted(pdir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            logging.debug("Skipping unreadable pursuit file %s", md, exc_info=True)
            continue
        if "sf_opportunity_id:" in text and opp_id in text:
            click.echo(md)
            return
