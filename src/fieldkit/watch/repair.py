"""repair-transition-dates — domain logic for repairing last-transition dates.

Moved from ``commands/watch/repair.py`` (watch-domain-migration, implementation change).
Contains the pure business logic for identifying and correcting pursuit
frontmatter files whose ``last-transition`` was incorrectly set to today.
No Click imports — CLI wiring stays in ``commands/watch/repair.py``.
"""

import datetime
import logging
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TypedDict

from fieldkit.config import TIMEOUT_REPAIR, get_fieldkit_home
from fieldkit.pursuit.io import load_pursuit, write_frontmatter

_log = logging.getLogger(__name__)


class RepairReport(TypedDict):
    path: Path
    account: str
    pursuit: str
    current_date: datetime.date
    proposed_date: datetime.date
    written: bool


@dataclass(frozen=True)
class RepairRunResult:
    repairs: tuple[RepairReport, ...]
    skipped: tuple[Path, ...]


# ---------------------------------------------------------------------------
# Path helpers (same pattern as pursuit_stalls.py)
# ---------------------------------------------------------------------------


@cache
def _accounts_dir() -> Path:
    return get_fieldkit_home() / "accounts"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_last_commit_date(path: Path) -> datetime.date | None:
    """Return the date of the last git commit that touched *path*.

    Returns None if the file is not tracked or git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=path.parent,
            timeout=TIMEOUT_REPAIR,
        )
    except FileNotFoundError:
        _log.debug("git not available; cannot determine last commit date for %s", path)
        return None

    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        _log.debug("Unexpected git date output %r for %s", raw, path)
        return None


def _file_mtime_date(path: Path) -> datetime.date:
    """Return the file modification date (local time) as a fallback."""
    return datetime.date.fromtimestamp(path.stat().st_mtime)


# ---------------------------------------------------------------------------
# Core scanning logic
# ---------------------------------------------------------------------------


def _all_pursuit_files(account: str | None = None) -> list[Path]:
    """Return sorted *.md files under accounts/*/pursuits/, optionally one account.

    Args:
        account: Restrict to ``accounts/<account>/pursuits/`` when given.
            The slug is validated at the CLI boundary, so an unknown one cannot
            reach here; a configured-but-empty account correctly yields [].
    """
    accounts = _accounts_dir()
    if not accounts.exists():
        return []
    pattern = "*/pursuits" if account is None else f"{account}/pursuits"
    files: list[Path] = []
    for pursuit_dir in sorted(accounts.glob(pattern)):
        files.extend(sorted(pursuit_dir.glob("*.md")))
    return files


def _repair_one(
    path: Path,
    today: datetime.date,
    *,
    dry_run: bool,
) -> RepairReport | None:
    """Inspect a single pursuit file and return a repair report dict or None.

    Returns None if the file doesn't need repair.

    Report keys:
        path, account, pursuit, current_date, proposed_date, written
    """
    try:
        model, body, mtime = load_pursuit(path)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not load %s: %s", path, exc)
        return None

    current = model.last_transition
    if current is None:
        return None

    # Normalise to date object
    if isinstance(current, str):
        try:
            current_date = datetime.date.fromisoformat(current)
        except ValueError:
            _log.debug("Non-ISO last-transition %r in %s — skipping", current, path)
            return None
    elif isinstance(current, datetime.date):
        current_date = current
    else:
        return None

    if current_date != today:
        return None  # not affected

    # Determine best-guess correct date:
    # 1. Last git commit date for this file
    # 2. Fall back to file mtime (may still be today if file was touched)
    proposed_date = _git_last_commit_date(path)
    if proposed_date is None or proposed_date == today:
        # git says today (or unavailable) — use mtime as best effort
        mtime_date = _file_mtime_date(path)
        if mtime_date < today:
            proposed_date = mtime_date
        else:
            # Both git and mtime say today — we cannot improve the date
            _log.debug(
                "Cannot determine pre-today date for %s (git=%s, mtime=%s) — skipping",
                path,
                proposed_date,
                mtime_date,
            )
            return None

    parts = path.parts
    try:
        account = parts[-3]
        pursuit = path.stem
    except IndexError:
        account = "unknown"
        pursuit = path.stem

    if not dry_run:
        model.last_transition = proposed_date
        try:
            write_frontmatter(path, model, body, expected_mtime=mtime)
            written = True
        except Exception as exc:  # noqa: BLE001
            _log.error("Failed to write %s: %s", path, exc)
            written = False
    else:
        written = False

    return {
        "path": path,
        "account": account,
        "pursuit": pursuit,
        "current_date": current_date,
        "proposed_date": proposed_date,
        "written": written,
    }


def _collect_repairs(*, dry_run: bool, account: str | None) -> RepairRunResult:
    files = _all_pursuit_files(account)
    today = datetime.datetime.now(tz=datetime.UTC).date()
    mode = "dry-run" if dry_run else "apply"
    _log.info("repair-transition-dates mode=%s date=%s files=%d", mode, today, len(files))
    repairs: list[RepairReport] = []
    skipped: list[Path] = []
    for path in files:
        report = _repair_one(path, today, dry_run=dry_run)
        if report is None:
            skipped.append(path)
        else:
            repairs.append(report)
    return RepairRunResult(tuple(repairs), tuple(skipped))


def _run_repair(*, dry_run: bool, verbose: bool, account: str | None = None, as_json: bool = False) -> int:
    """Run the repair-transition-dates watcher.

    Iterates over all pursuit files, repairs incorrect last-transition dates,
    and returns an exit code.

    Args:
        dry_run: If True, print proposals without writing.
        verbose: If True, also print files that are already correct.
        account: Restrict the sweep to a single account slug.

    Returns:
        0 on success.
    """
    import click

    result = _collect_repairs(dry_run=dry_run, account=account)
    if as_json:
        import json

        click.echo(
            json.dumps(
                {
                    "dry_run": dry_run,
                    "repairs": list(result.repairs),
                    "repair_count": len(result.repairs),
                    "skipped_count": len(result.skipped),
                    "account": account,
                },
                default=str,
                sort_keys=True,
            )
        )
        return 0

    if verbose:
        for path in result.skipped:
            click.echo(f"  ok      {path}")
    for report in result.repairs:
        verb = "would update" if dry_run else ("updated" if report["written"] else "ERROR")
        click.echo(
            f"  {verb:13s}  {report['account']}/{report['pursuit']}"
            f"  {report['current_date']} → {report['proposed_date']}"
        )

    action = "would be updated" if dry_run else "updated"
    click.echo(f"\nSummary: {len(result.repairs)} pursuit(s) {action}, {len(result.skipped)} already correct.")

    if dry_run and result.repairs:
        click.echo("Run with --apply to write changes.")

    return 0
