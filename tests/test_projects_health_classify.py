"""Tests for classify_project() — covering uncovered branches.

cc=13, cov=68%, target: ZOMBIE, EXPIRING, SOON, ACTIVE, UNKNOWN,
completed+past, OSError, no frontmatter.
"""

from datetime import date
from pathlib import Path

import pytest

from fieldkit.pursuit.projects import classify_project

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_project(path: Path, frontmatter: str, body: str = "# Project\n") -> None:
    """Write project file; quote date fields to prevent YAML auto-parsing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure sf_contract_end values are quoted strings so YAML doesn't auto-parse
    # them as datetime.date objects (which _parse_iso_date won't accept)
    import re

    frontmatter = re.sub(
        r"(sf_contract_end:\s*)(\d{4}-\d{2}-\d{2})",
        r'\1"\2"',
        frontmatter,
    )
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


TODAY = date(2026, 6, 19)


# ---------------------------------------------------------------------------
# UNKNOWN — no end date
# ---------------------------------------------------------------------------


# ── TestClassifyProjectUnknown (flattened) ──────────────────────────────────


def test_classify_project_no_contract_end_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "no-end.md"
    _write_project(path, "sf_stage: Implementation\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "UNKNOWN"
    assert row.days_until_end is None


def test_classify_project_empty_contract_end_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "empty-end.md"
    _write_project(path, "sf_stage: Implementation\nsf_contract_end: \n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "UNKNOWN"


def test_classify_project_empty_frontmatter_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "empty-frontmatter.md"
    path.write_text("---\n---\n# Project\n", encoding="utf-8")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "UNKNOWN"
    assert row.days_until_end is None


# ---------------------------------------------------------------------------
# ZOMBIE — past end date, not completed
# ---------------------------------------------------------------------------


# ── TestClassifyProjectZombie (flattened) ───────────────────────────────────


def test_classify_project_past_end_active_stage_is_zombie(tmp_path: Path) -> None:
    path = tmp_path / "zombie-project.md"
    _write_project(
        path,
        "sf_stage: Implementation\nsf_contract_end: 2025-01-01\n",
    )

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "ZOMBIE"
    assert row.days_until_end is not None
    assert row.days_until_end < 0


def test_classify_project_past_end_delivery_stage_is_zombie(tmp_path: Path) -> None:
    path = tmp_path / "delivery-zombie.md"
    _write_project(
        path,
        "sf_stage: Delivery\nsf_contract_end: 2024-06-01\n",
    )

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "ZOMBIE"


# ---------------------------------------------------------------------------
# ACTIVE — completed/closed past end date (not zombie)
# ---------------------------------------------------------------------------


# ── TestClassifyProjectCompletedPastEnd (flattened) ─────────────────────────


def test_classify_project_completed_past_end_is_active(tmp_path: Path) -> None:
    """Completed project past end date is ACTIVE, not ZOMBIE."""
    path = tmp_path / "done-project.md"
    _write_project(
        path,
        "sf_stage: Completed\nsf_contract_end: 2025-01-01\n",
    )

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "ACTIVE"


def test_classify_project_closed_past_end_is_active(tmp_path: Path) -> None:
    """Closed project past end date is ACTIVE, not ZOMBIE."""
    path = tmp_path / "closed-project.md"
    _write_project(
        path,
        "sf_stage: Closed\nsf_contract_end: 2025-06-01\n",
    )

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "ACTIVE"


# ---------------------------------------------------------------------------
# EXPIRING — within 30 days
# ---------------------------------------------------------------------------


# ── TestClassifyProjectExpiring (flattened) ─────────────────────────────────


def test_classify_project_10_days_until_end_is_expiring(tmp_path: Path) -> None:
    path = tmp_path / "expiring.md"
    close_date = date(TODAY.year, TODAY.month, TODAY.day)
    # 10 days from today
    import datetime

    end = (close_date + datetime.timedelta(days=10)).isoformat()
    _write_project(path, f"sf_stage: Implementation\nsf_contract_end: {end}\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "EXPIRING"
    assert row.days_until_end == 10


def test_classify_project_30_days_until_end_is_expiring(tmp_path: Path) -> None:
    import datetime

    path = tmp_path / "expiring-30.md"
    end = (date(TODAY.year, TODAY.month, TODAY.day) + datetime.timedelta(days=30)).isoformat()
    _write_project(path, f"sf_stage: Implementation\nsf_contract_end: {end}\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "EXPIRING"


# ---------------------------------------------------------------------------
# SOON — within 90 days but not 30
# ---------------------------------------------------------------------------


# ── TestClassifyProjectSoon (flattened) ─────────────────────────────────────


def test_classify_project_60_days_until_end_is_soon(tmp_path: Path) -> None:
    import datetime

    path = tmp_path / "soon.md"
    end = (date(TODAY.year, TODAY.month, TODAY.day) + datetime.timedelta(days=60)).isoformat()
    _write_project(path, f"sf_stage: Implementation\nsf_contract_end: {end}\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "SOON"
    assert row.days_until_end == 60


def test_classify_project_90_days_until_end_is_soon(tmp_path: Path) -> None:
    import datetime

    path = tmp_path / "soon-90.md"
    end = (date(TODAY.year, TODAY.month, TODAY.day) + datetime.timedelta(days=90)).isoformat()
    _write_project(path, f"sf_stage: Implementation\nsf_contract_end: {end}\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "SOON"


# ---------------------------------------------------------------------------
# ACTIVE — more than 90 days
# ---------------------------------------------------------------------------


# ── TestClassifyProjectActive (flattened) ───────────────────────────────────


def test_classify_project_180_days_until_end_is_active(tmp_path: Path) -> None:
    import datetime

    path = tmp_path / "active.md"
    end = (date(TODAY.year, TODAY.month, TODAY.day) + datetime.timedelta(days=180)).isoformat()
    _write_project(path, f"sf_stage: Implementation\nsf_contract_end: {end}\n")

    row = classify_project(path, tmp_path, TODAY)

    assert row is not None
    assert row.health == "ACTIVE"
    assert row.days_until_end == 180


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


# ── TestClassifyProjectErrors (flattened) ───────────────────────────────────


def test_classify_project_oserror_returns_none(tmp_path: Path) -> None:
    """Unreadable file returns None (OSError swallowed)."""
    path = tmp_path / "nonexistent.md"
    # File does not exist → OSError

    row = classify_project(path, tmp_path, TODAY)

    assert row is None


def test_classify_project_no_frontmatter_returns_none(tmp_path: Path) -> None:
    """File with no YAML frontmatter returns None."""
    path = tmp_path / "no-fm.md"
    path.write_text("# Just a heading\n\nNo frontmatter here.\n", encoding="utf-8")

    row = classify_project(path, tmp_path, TODAY)

    assert row is None


# ---------------------------------------------------------------------------
# ProjectRow fields
# ---------------------------------------------------------------------------


# ── TestClassifyProjectRowFields (flattened) ────────────────────────────────


def test_classify_project_row_fields_populated_correctly(tmp_path: Path) -> None:
    """ProjectRow is populated with correct field values."""
    import datetime

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    path = accounts_dir / "acme-corp" / "projects" / "proj-a.md"
    end = (date(TODAY.year, TODAY.month, TODAY.day) + datetime.timedelta(days=60)).isoformat()
    _write_project(
        path,
        f"sf_stage: Implementation\nsf_contract_end: {end}\nsf_opportunity: OPP-001\n",
    )

    row = classify_project(path, accounts_dir, TODAY)

    assert row is not None
    assert row.sf_stage == "Implementation"
    assert row.contract_end_str == end
    assert row.opportunity == "OPP-001"
    assert row.health == "SOON"
    assert row.days_until_end == 60
    assert "acme-corp" in row.name


# ---------------------------------------------------------------------------
# _format_days helper
# ---------------------------------------------------------------------------


# ── TestFormatDays (flattened) ──────────────────────────────────────────────


def test_format_days_none_days_returns_dash() -> None:
    from fieldkit.commands.pursuit.projects_health import _format_days
    from fieldkit.pursuit.projects import ProjectRow

    row = ProjectRow(
        relative_path="p.md",
        name="proj",
        sf_stage="",
        contract_end_str="",
        days_until_end=None,
        health="UNKNOWN",
        opportunity="",
    )
    assert _format_days(row) == "—"


def test_format_days_negative_days_returns_ovr() -> None:
    from fieldkit.commands.pursuit.projects_health import _format_days
    from fieldkit.pursuit.projects import ProjectRow

    row = ProjectRow(
        relative_path="p.md",
        name="proj",
        sf_stage="",
        contract_end_str="",
        days_until_end=-5,
        health="ZOMBIE",
        opportunity="",
    )
    assert _format_days(row) == "-5d OVR"


def test_format_days_positive_days_returns_string() -> None:
    from fieldkit.commands.pursuit.projects_health import _format_days
    from fieldkit.pursuit.projects import ProjectRow

    row = ProjectRow(
        relative_path="p.md",
        name="proj",
        sf_stage="",
        contract_end_str="",
        days_until_end=30,
        health="EXPIRING",
        opportunity="",
    )
    assert _format_days(row) == "30"


# ---------------------------------------------------------------------------
# _parse_iso_date edge cases
# ---------------------------------------------------------------------------


# ── TestParseIsoDate (flattened) ────────────────────────────────────────────


def test_parse_iso_date_empty_string_returns_none() -> None:
    from fieldkit.pursuit.projects import _parse_iso_date

    assert _parse_iso_date("") is None


def test_parse_iso_date_none_returns_none() -> None:
    from fieldkit.pursuit.projects import _parse_iso_date

    assert _parse_iso_date(None) is None


def test_parse_iso_date_invalid_format_returns_none() -> None:
    from fieldkit.pursuit.projects import _parse_iso_date

    assert _parse_iso_date("not-a-date") is None


def test_parse_iso_date_valid_date_returns_date() -> None:
    from datetime import date

    from fieldkit.pursuit.projects import _parse_iso_date

    result = _parse_iso_date("2026-06-19")
    assert result == date(2026, 6, 19)


# ---------------------------------------------------------------------------
# _extract_project_name — path not relative to accounts_dir
# ---------------------------------------------------------------------------


# ── TestExtractProjectName (flattened) ──────────────────────────────────────


def test_extract_project_name_path_not_under_accounts_dir_returns_stem(tmp_path: Path) -> None:
    """When path is not under accounts_dir, returns path.stem."""
    from fieldkit.pursuit.projects import _extract_project_name

    path = tmp_path / "some-project.md"
    accounts_dir = tmp_path / "accounts"  # different dir

    result = _extract_project_name(path, accounts_dir)

    assert result == "some-project"


# ---------------------------------------------------------------------------
# _parse_iso_date — whitespace-only string
# ---------------------------------------------------------------------------


# ── TestParseIsoDateWhitespace (flattened) ──────────────────────────────────


def test_parse_iso_date_whitespace_only_returns_none() -> None:
    """Whitespace-only string returns None (stripped to empty)."""
    from fieldkit.pursuit.projects import _parse_iso_date

    assert _parse_iso_date("   ") is None


def test_parse_iso_date_non_string_returns_none() -> None:
    """Non-string input (e.g. int) returns None."""
    from fieldkit.pursuit.projects import _parse_iso_date

    assert _parse_iso_date(42) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# health_check — edge cases
# ---------------------------------------------------------------------------


# ── TestHealthCheck (flattened) ─────────────────────────────────────────────


def test_health_check_today_defaults_to_today(tmp_path: Path) -> None:
    """health_check with today=None uses date.today() without error."""
    from fieldkit.pursuit.projects import health_check

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()

    # today=None triggers the default branch
    result = health_check(tmp_path, today=None)

    assert result == []  # no project files


def test_health_check_no_accounts_dir_returns_empty(tmp_path: Path) -> None:
    """health_check returns [] when accounts/ dir does not exist."""
    from fieldkit.pursuit.projects import health_check

    # tmp_path has no accounts/ subdirectory
    result = health_check(tmp_path)

    assert result == []


def test_health_check_account_filter_applied(tmp_path: Path) -> None:
    """account_filter limits scan to the specified account directory."""
    import datetime

    from fieldkit.pursuit.projects import health_check

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    proj_dir = accounts_dir / "acme-corp" / "projects"
    proj_dir.mkdir(parents=True)
    end = (TODAY + datetime.timedelta(days=60)).isoformat()
    (proj_dir / "proj-a.md").write_text(
        f'---\nsf_stage: Implementation\nsf_contract_end: "{end}"\n---\n# Project\n',
        encoding="utf-8",
    )

    result = health_check(tmp_path, account_filter="acme-corp", today=TODAY)

    assert len(result) == 1
    assert result[0].health == "SOON"


def test_health_check_template_files_excluded(tmp_path: Path) -> None:
    """Files named template.md are excluded from the scan."""
    from fieldkit.pursuit.projects import health_check

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    proj_dir = accounts_dir / "acme-corp" / "projects"
    proj_dir.mkdir(parents=True)
    (proj_dir / "template.md").write_text(
        "---\nsf_stage: Implementation\n---\n# Template\n",
        encoding="utf-8",
    )

    result = health_check(tmp_path, today=TODAY)

    assert result == []


# ---------------------------------------------------------------------------
# _print_health_table — formatting
# ---------------------------------------------------------------------------


# ── TestPrintHealthTable (flattened) ────────────────────────────────────────


def test_print_health_table_prints_without_error(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_health_table prints a formatted table without raising."""

    from fieldkit.commands.pursuit.projects_health import _print_health_table
    from fieldkit.pursuit.projects import ProjectRow

    rows = [
        ProjectRow(
            relative_path="acme-corp/projects/proj-a.md",
            name="acme-corp/proj-a",
            sf_stage="Implementation",
            contract_end_str="2026-09-01",
            days_until_end=74,
            health="SOON",
            opportunity="OPP-001",
        ),
        ProjectRow(
            relative_path="acme-corp/projects/zombie.md",
            name="acme-corp/zombie",
            sf_stage="Delivery",
            contract_end_str="2025-01-01",
            days_until_end=-170,
            health="ZOMBIE",
            opportunity="",
        ),
    ]
    _print_health_table(rows, TODAY)
    captured = capsys.readouterr()
    assert "SOON" in captured.out
    assert "ZOMBIE" in captured.out
    assert "acme-corp/proj-a" in captured.out
