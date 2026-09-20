"""Tests for audit --check-yaml duplicate YAML key detection."""

from pathlib import Path

import pytest

from fieldkit.commands.pursuit.audit import check_yaml_duplicates, check_yaml_duplicates_directory

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLEAN_FRONTMATTER = """\
---
stage: discover
gate-status: pending
sf_opportunity_id: "006Pe000012n2GkIAI"
sf_stage: Discover
---

# Clean Pursuit
"""

DUPLICATE_FRONTMATTER = """\
---
stage: discover
gate-status: pending
sf_opportunity_id: "006Pe000012n2GkIAI"
sf_stage: Discover
sf_stage: Qualify
---

# Pursuit With Duplicate Key
"""

DOUBLE_DUPLICATE_FRONTMATTER = """\
---
stage: discover
stage: qualify
sf_opportunity_id: "006Pe000012n2GkIAI"
sf_stage: Discover
sf_stage: Qualify
---

# Pursuit With Two Duplicate Keys
"""

NO_FRONTMATTER = """\
# No Frontmatter

Just body text.
"""


def _make_file(tmp_path: Path, content: str, name: str = "test-pursuit.md") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# check_yaml_duplicates — single-file function
# ---------------------------------------------------------------------------


def test_clean_file_no_findings(tmp_path: Path) -> None:
    path = _make_file(tmp_path, CLEAN_FRONTMATTER)
    result = check_yaml_duplicates(path)
    assert not result.findings
    assert result.parse_error is None


def test_duplicate_key_returns_error_finding(tmp_path: Path) -> None:
    path = _make_file(tmp_path, DUPLICATE_FRONTMATTER)
    result = check_yaml_duplicates(path)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.level == "ERROR"
    assert "sf_stage" in finding.message


def test_two_duplicate_keys_each_reported(tmp_path: Path) -> None:
    path = _make_file(tmp_path, DOUBLE_DUPLICATE_FRONTMATTER)
    result = check_yaml_duplicates(path)
    assert len(result.findings) == 2
    keys_reported = {f.message for f in result.findings}
    assert any("stage" in m for m in keys_reported)
    assert any("sf_stage" in m for m in keys_reported)


def test_no_frontmatter_sets_parse_error(tmp_path: Path) -> None:
    path = _make_file(tmp_path, NO_FRONTMATTER)
    result = check_yaml_duplicates(path)
    assert result.parse_error is not None
    assert not result.findings


# ---------------------------------------------------------------------------
# check_yaml_duplicates_directory — directory-level function
# ---------------------------------------------------------------------------


def _make_pursuit_tree(tmp_path: Path, account: str, filename: str, content: str) -> Path:
    """Create tmp_path/accounts/<account>/pursuits/<filename>."""
    pursuits_dir = tmp_path / "accounts" / account / "pursuits"
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    path = pursuits_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_directory_clean_files_returns_empty(tmp_path: Path) -> None:
    _make_pursuit_tree(tmp_path, "acme", "deal-a.md", CLEAN_FRONTMATTER)
    _make_pursuit_tree(tmp_path, "acme", "deal-b.md", CLEAN_FRONTMATTER)
    results = check_yaml_duplicates_directory(tmp_path)
    assert results == []


def test_directory_bad_file_returns_findings(tmp_path: Path) -> None:
    _make_pursuit_tree(tmp_path, "acme", "deal-bad.md", DUPLICATE_FRONTMATTER)
    results = check_yaml_duplicates_directory(tmp_path)
    assert len(results) == 1
    assert results[0].findings


def test_directory_mixed_files_only_bad_returned(tmp_path: Path) -> None:
    _make_pursuit_tree(tmp_path, "acme", "deal-clean.md", CLEAN_FRONTMATTER)
    _make_pursuit_tree(tmp_path, "acme", "deal-bad.md", DUPLICATE_FRONTMATTER)
    results = check_yaml_duplicates_directory(tmp_path)
    assert len(results) == 1
    assert "deal-bad.md" in results[0].relative_path


def test_directory_account_filter_limits_scope(tmp_path: Path) -> None:
    _make_pursuit_tree(tmp_path, "acme", "deal.md", DUPLICATE_FRONTMATTER)
    _make_pursuit_tree(tmp_path, "other", "deal.md", DUPLICATE_FRONTMATTER)
    results = check_yaml_duplicates_directory(tmp_path, account_filter="acme")
    assert len(results) == 1
    assert "acme" in results[0].relative_path


def test_directory_missing_accounts_dir_returns_empty(tmp_path: Path) -> None:
    # tmp_path has no accounts/ subdirectory
    results = check_yaml_duplicates_directory(tmp_path)
    assert results == []


def test_directory_skips_gmail_intel_and_template(tmp_path: Path) -> None:
    _make_pursuit_tree(tmp_path, "acme", "gmail-intel.md", DUPLICATE_FRONTMATTER)
    _make_pursuit_tree(tmp_path, "acme", "template.md", DUPLICATE_FRONTMATTER)
    results = check_yaml_duplicates_directory(tmp_path)
    assert results == []
