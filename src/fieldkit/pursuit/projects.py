"""fieldkit.pursuit.projects — Delivery project health check.

Scans accounts/*/projects/*.md files and classifies each engagement by health:
  ZOMBIE    — contract end date in the past and stage != Completed/Closed
  EXPIRING  — contract end date within 30 days
  SOON      — contract end date within 90 days
  ACTIVE    — contract end date > 90 days out
  UNKNOWN   — no end date in frontmatter

Public API:
  ProjectRow: single project's health data
  health_check(root, account_filter, today) → list[ProjectRow]
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fieldkit.pursuit.io import parse_frontmatter_fallback

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPLETED_STAGES = frozenset({"Completed", "Closed", "completed", "closed"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProjectRow:
    relative_path: str
    name: str
    sf_stage: str
    contract_end_str: str
    days_until_end: int | None  # None = no end date; negative = past
    health: str  # "ZOMBIE", "EXPIRING", "SOON", "ACTIVE", "UNKNOWN"
    opportunity: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_iso_date(raw: object) -> date | None:
    """Parse YYYY-MM-DD string to date, or None."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    with contextlib.suppress(ValueError):
        return datetime.strptime(s, "%Y-%m-%d").date()
    return None


def _extract_project_name(path: Path, accounts_dir: Path) -> str:
    """Derive a short display name from the project file path."""
    with contextlib.suppress(ValueError):
        rel = str(path.relative_to(accounts_dir))
        return rel.replace("/projects/", "/").replace(".md", "")
    return path.stem


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_project(
    path: Path,
    accounts_dir: Path,
    today: date,
) -> ProjectRow | None:
    """Parse a project file and classify its health.

    Returns None if the file cannot be parsed or has no meaningful data.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, _ = parse_frontmatter_fallback(content)
    if fm is None:
        return None

    sf_stage = str(fm.get("sf_stage") or "")
    end_date = _parse_iso_date(fm.get("sf_contract_end"))
    opportunity = str(fm.get("sf_opportunity") or "")
    name = _extract_project_name(path, accounts_dir)
    end_str = str(fm.get("sf_contract_end") or "")

    days: int | None = None
    if end_date:
        days = (end_date - today).days

    if days is None:
        health = "UNKNOWN"
    elif days < 0 and sf_stage not in COMPLETED_STAGES:
        health = "ZOMBIE"
    elif days < 0:
        health = "ACTIVE"  # Completed/Closed, just past end date — normal
    elif days <= 30:
        health = "EXPIRING"
    elif days <= 90:
        health = "SOON"
    else:
        health = "ACTIVE"

    return ProjectRow(
        relative_path=str(path.relative_to(accounts_dir)) if accounts_dir in path.parents else str(path),
        name=name,
        sf_stage=sf_stage,
        contract_end_str=end_str,
        days_until_end=days,
        health=health,
        opportunity=opportunity,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def health_check(
    root: Path,
    account_filter: str | None = None,
    today: date | None = None,
) -> list[ProjectRow]:
    """Run a health check across all project files.

    Args:
        root:           Data root directory (contains `accounts/` subdirectory).
        account_filter: If set, only check files under accounts/<account_filter>/.
        today:          Reference date (defaults to today).

    Returns:
        List of ProjectRow sorted: ZOMBIE first, then EXPIRING, SOON, ACTIVE, UNKNOWN.
        Within tiers, sorted by days_until_end ascending.
    """
    if today is None:
        today = datetime.now(tz=UTC).date()

    accounts_dir = root / "accounts"
    if not accounts_dir.is_dir():
        return []

    rows: list[ProjectRow] = []

    # historic regression: exclude dot-directories (e.g. .archive/) and template files.
    # When an account_filter is given we can use a narrow glob; the dot-dir
    # guard is still applied for consistency.
    pattern = f"{account_filter}/projects/*.md" if account_filter else "*/projects/*.md"
    for path in sorted(
        p
        for p in accounts_dir.glob(pattern)
        if not p.parts[len(accounts_dir.parts)].startswith(".") and p.stem != "template"
    ):
        row = classify_project(path, accounts_dir, today)
        if row is not None:
            rows.append(row)

    health_order = {"ZOMBIE": 0, "EXPIRING": 1, "SOON": 2, "ACTIVE": 3, "UNKNOWN": 4}

    def sort_key(r: ProjectRow) -> tuple[int, int]:
        d = r.days_until_end if r.days_until_end is not None else 9999
        return (health_order.get(r.health, 5), d)

    return sorted(rows, key=sort_key)
