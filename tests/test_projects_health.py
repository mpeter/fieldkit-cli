"""Tests for fieldkit.pursuit.projects_health — engagement health classification."""

from datetime import date
from pathlib import Path

import pytest

from fieldkit.pursuit.projects import _parse_iso_date, classify_project, health_check

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_FM = """\
---
sf_record_id: "a74Pe000001W8oH"
sf_stage: "In Progress"
sf_contract_start: "2026-01-06"
sf_contract_end: "{end_date}"
sf_opportunity: "Test Opportunity"
sf_opa_number: "12345"
sf_last_pulled: "2026-05-11"
---

# Test Project
"""

TODAY = date(2026, 6, 1)


def _make_project(tmp_path: Path, content: str, account: str = "acme", name: str = "proj.md") -> Path:
    p = tmp_path / "accounts" / account / "projects"
    p.mkdir(parents=True, exist_ok=True)
    path = p / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------


def test_parse_iso_date_valid() -> None:
    assert _parse_iso_date("2026-08-15") == date(2026, 8, 15)


def test_parse_iso_date_empty() -> None:
    assert _parse_iso_date("") is None
    assert _parse_iso_date(None) is None


def test_parse_iso_date_invalid() -> None:
    assert _parse_iso_date("not-a-date") is None


# ---------------------------------------------------------------------------
# classify_project
# ---------------------------------------------------------------------------


def test_classify_active(tmp_path: Path) -> None:
    content = BASE_FM.format(end_date="2026-10-01")
    p = _make_project(tmp_path, content)
    accounts_dir = tmp_path / "accounts"
    row = classify_project(p, accounts_dir, TODAY)
    assert row is not None
    assert row.health == "ACTIVE"
    assert row.days_until_end == (date(2026, 10, 1) - TODAY).days


def test_classify_soon(tmp_path: Path) -> None:
    # 60 days out
    content = BASE_FM.format(end_date="2026-07-31")
    p = _make_project(tmp_path, content)
    row = classify_project(p, tmp_path / "accounts", TODAY)
    assert row is not None
    assert row.health == "SOON"


def test_classify_expiring(tmp_path: Path) -> None:
    # 14 days out
    content = BASE_FM.format(end_date="2026-06-15")
    p = _make_project(tmp_path, content)
    row = classify_project(p, tmp_path / "accounts", TODAY)
    assert row is not None
    assert row.health == "EXPIRING"


def test_classify_zombie(tmp_path: Path) -> None:
    # Past end date, still "In Progress"
    content = BASE_FM.format(end_date="2026-04-01")
    p = _make_project(tmp_path, content)
    row = classify_project(p, tmp_path / "accounts", TODAY)
    assert row is not None
    assert row.health == "ZOMBIE"
    assert row.days_until_end is not None and row.days_until_end < 0


def test_classify_completed_past_end_not_zombie(tmp_path: Path) -> None:
    # Past end date but stage=Completed → ACTIVE (not ZOMBIE)
    content = BASE_FM.format(end_date="2026-04-01").replace('"In Progress"', '"Completed"')
    p = _make_project(tmp_path, content)
    row = classify_project(p, tmp_path / "accounts", TODAY)
    assert row is not None
    assert row.health == "ACTIVE"


def test_classify_unknown_no_end_date(tmp_path: Path) -> None:
    content = BASE_FM.format(end_date="")
    p = _make_project(tmp_path, content)
    row = classify_project(p, tmp_path / "accounts", TODAY)
    assert row is not None
    assert row.health == "UNKNOWN"
    assert row.days_until_end is None


def test_classify_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "accounts" / "acme" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    f = p / "bare.md"
    f.write_text("# No frontmatter", encoding="utf-8")
    row = classify_project(f, tmp_path / "accounts", TODAY)
    assert row is None


# ---------------------------------------------------------------------------
# health_check — directory scan
# ---------------------------------------------------------------------------


def test_health_check_scans_projects(tmp_path: Path) -> None:
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"))
    rows = health_check(tmp_path, today=TODAY)
    assert len(rows) == 1
    assert rows[0].health == "ACTIVE"


def test_health_check_sorted_zombie_first(tmp_path: Path) -> None:
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), name="active.md")
    _make_project(tmp_path, BASE_FM.format(end_date="2026-04-01"), name="zombie.md")
    rows = health_check(tmp_path, today=TODAY)
    assert rows[0].health == "ZOMBIE"
    assert rows[1].health == "ACTIVE"


def test_health_check_account_filter(tmp_path: Path) -> None:
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme")
    _make_project(tmp_path, BASE_FM.format(end_date="2026-04-01"), account="globex")
    rows = health_check(tmp_path, account_filter="acme", today=TODAY)
    assert len(rows) == 1
    assert "acme" in rows[0].relative_path


def test_health_check_no_projects_dir(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = health_check(tmp_path, today=TODAY)
    assert rows == []


def test_health_check_relative_path(tmp_path: Path) -> None:
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="my-project.md")
    rows = health_check(tmp_path, today=TODAY)
    assert rows[0].relative_path == "acme/projects/my-project.md"


# ---------------------------------------------------------------------------
# historic regression: template files and dot-directories excluded from projects_health scan
# ---------------------------------------------------------------------------


def test_health_check_excludes_template_file(tmp_path: Path) -> None:
    """historic regression: template.md must not appear in health_check results."""
    # Real project — should appear
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="real-project.md")
    # Template file — must be excluded
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="template.md")
    rows = health_check(tmp_path, today=TODAY)
    names = [r.name for r in rows]
    assert any("real-project" in n for n in names), "Real project must be returned"
    assert not any("template" in n for n in names), "template.md must be excluded"


def test_health_check_excludes_dot_directory(tmp_path: Path) -> None:
    """historic regression: projects under dot-prefixed account directories must be excluded."""
    # Real project under a normal account
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="proj.md")
    # Project under a dot-directory (e.g. .archive)
    _make_project(tmp_path, BASE_FM.format(end_date="2026-04-01"), account=".archive", name="old-proj.md")
    rows = health_check(tmp_path, today=TODAY)
    # Only the acme project should be returned
    assert len(rows) == 1
    assert "acme" in rows[0].relative_path
    assert ".archive" not in rows[0].relative_path


def test_health_check_template_excluded_with_account_filter(tmp_path: Path) -> None:
    """historic regression: template filter applies even when account_filter is set."""
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="real.md")
    _make_project(tmp_path, BASE_FM.format(end_date="2026-10-01"), account="acme", name="template.md")
    rows = health_check(tmp_path, account_filter="acme", today=TODAY)
    assert len(rows) == 1
    assert "real" in rows[0].name
