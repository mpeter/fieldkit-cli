"""Tests for fieldkit.pursuit.projects_health — delivery project health classification.

Task 10.4: parametrized classification tests for classify_project().
"""

from datetime import date
from pathlib import Path

import pytest

from fieldkit.pursuit.projects import ProjectRow, classify_project

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_project(tmp_path: Path, sf_stage: str, sf_contract_end: str, name: str = "project.md") -> Path:
    """Write a minimal project file with the given stage and contract end date.

    The contract end date is quoted so YAML parses it as a string rather than
    a datetime.date object — _parse_iso_date() only accepts str inputs.
    """
    content = f"""\
---
sf_stage: {sf_stage}
sf_contract_end: '{sf_contract_end}'
sf_opportunity: OPP-001
---

# Test Project
"""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Task 10.4 — classify_project parametrized across health states
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "sf_stage, sf_contract_end, today, expected_health",
    [
        # ACTIVE: contract end date > 90 days out
        (
            "Delivery",
            "2027-12-31",
            date(2026, 6, 14),
            "ACTIVE",
        ),
        # EXPIRING: contract end date within 30 days (not completed)
        (
            "Delivery",
            "2026-06-30",
            date(2026, 6, 14),
            "EXPIRING",
        ),
        # ZOMBIE: contract end date in the past, stage not completed
        (
            "Delivery",
            "2025-01-01",
            date(2026, 6, 14),
            "ZOMBIE",
        ),
    ],
    ids=["on-track", "at-risk", "overdue"],
)
def test_classify_project_returns_correct_classification(
    tmp_path: Path,
    sf_stage: str,
    sf_contract_end: str,
    today: date,
    expected_health: str,
) -> None:
    """classify_project assigns the correct health label for each contract state.

    Three parametrized cases:
      on-track  — ACTIVE (end date > 90 days out)
      at-risk   — EXPIRING (end date within 30 days)
      overdue   — ZOMBIE (end date in the past, stage not completed)
    """
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True)
    project_file = _write_project(tmp_path, sf_stage, sf_contract_end)

    row = classify_project(project_file, accounts_dir, today)

    assert row is not None, "classify_project must return a ProjectRow, not None"
    assert isinstance(row, ProjectRow)
    assert row.health == expected_health, (
        f"Expected health={expected_health!r} for stage={sf_stage!r}, "
        f"end={sf_contract_end!r}, today={today}; got {row.health!r}"
    )
