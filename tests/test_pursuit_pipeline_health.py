"""Tests for fieldkit.pursuit.pipeline_health — pipeline risk classification.

Task 10.5: contract tests for classify_pursuit() and the health CLI command.
"""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.audit import audit_file
from fieldkit.commands.pursuit.pipeline_health import RiskItem, classify_pursuit
from fieldkit.commands.pursuit.pipeline_health import cli as health_cli

# ---------------------------------------------------------------------------
# Shared frontmatter templates
# ---------------------------------------------------------------------------

# A pursuit with an overdue close date → HIGH risk
_HIGH_RISK_FRONTMATTER = """\
---
stage: discover
gate-status: pending
last-transition: 2026-01-01
transition-history: []
meddpicc:
  metrics: 0
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  identify-pain: 0
  champion: 0
  competition: 0
  paper-process: 0
sf_opportunity_id: OPP-HIGH
sf_stage: Discover
sf_close_date: 2025-01-01
sf_arr: $50,000.00
sf_owner: Jane Doe
sf_next_steps: ''
sf_last_pulled: 2026-01-01T00:00:00Z
---

# High Risk Pursuit
"""

# A pursuit with a future close date and no independent risk → LOW risk
_LOW_RISK_FRONTMATTER = """\
---
stage: discover
gate-status: pending
last-transition: 2026-01-01
transition-history:
  - date: 2026-01-01
    from: ''
    to: discover
    gate-result: pass
    override-reason: ''
meddpicc:
  metrics: 2
  economic-buyer: 2
  decision-criteria: 2
  decision-process: 2
  identify-pain: 2
  champion: 2
  competition: 2
  paper-process: 2
sf_opportunity_id: OPP-LOW
sf_stage: Discover
sf_close_date: 12/31/2027
sf_arr: $100,000.00
sf_owner: Jane Doe
sf_next_steps: Schedule discovery call
sf_last_pulled: 2026-01-01T00:00:00Z
---

# Low Risk Pursuit
"""


def _write_pursuit(tmp_path: Path, content: str, name: str = "pursuit.md") -> Path:
    """Write a pursuit file and return its path."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Task 10.5 — classify_pursuit gate verdict
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "frontmatter, today, expected_tier",
    [
        # HIGH: overdue close date (2025-01-01 < today 2026-06-14)
        (_HIGH_RISK_FRONTMATTER, date(2026, 6, 14), "HIGH"),
        # LOW: future close date and no independent risk
        (_LOW_RISK_FRONTMATTER, date(2026, 6, 14), "LOW"),
    ],
    ids=["overdue-high-risk", "healthy-low-risk"],
)
def test_classify_pursuit_returns_correct_gate_verdict(
    tmp_path: Path,
    frontmatter: str,
    today: date,
    expected_tier: str,
) -> None:
    """classify_pursuit assigns the correct risk tier from independent risk evidence.

    Two parametrized cases:
      overdue-high-risk — close date in the past → HIGH
      healthy-low-risk  — future close date, no independent risk → LOW
    """
    p = _write_pursuit(tmp_path, frontmatter)
    audit_result = audit_file(p, today=today)

    item = classify_pursuit(audit_result, today)

    assert item is not None, "classify_pursuit must return a RiskItem, not None"
    assert isinstance(item, RiskItem)
    assert item.risk_tier == expected_tier, (
        f"Expected risk_tier={expected_tier!r}; got {item.risk_tier!r}. Reasons: {item.risk_reasons}"
    )


# ---------------------------------------------------------------------------
# Task 10.5 — pipeline health CLI exits 0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_health_cli_exits_zero(tmp_path: Path) -> None:
    """pipeline health CLI exits 0 when active pursuit files are found.

    The CLI always exits 0 (HIGH risk is informational, not a command failure).
    """
    # Set up a data root with one active pursuit file
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(_LOW_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, [], catch_exceptions=False)

    assert result.exit_code == 0, (
        f"Expected exit 0 (health CLI always exits 0 per spec), got {result.exit_code}:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# Additional CLI coverage tests (CRAP reduction)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_health_cli_json_flag_emits_valid_json_and_exits_0(tmp_path: Path) -> None:
    """--json emits a valid JSON array and succeeds when HIGH items exist."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "high.md").write_text(_HIGH_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, ["--json"], catch_exceptions=False)

    assert result.exit_code == 0, f"Expected exit 0 for a valid report, got {result.exit_code}:\n{result.output}"
    import json

    parsed = json.loads(result.output)
    assert isinstance(parsed, list), f"Expected JSON array, got: {type(parsed)}"
    assert len(parsed) >= 1
    assert parsed[0]["risk_tier"] == "HIGH"
    assert parsed[0]["qualification_status"] == "unavailable"
    assert "meddpicc_score" not in parsed[0]


@pytest.mark.unit
@pytest.mark.parametrize("extra_args", [[], ["--json"]])
def test_pipeline_health_cli_strict_high_exits_1_with_complete_report(tmp_path: Path, extra_args: list[str]) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "high.md").write_text(_HIGH_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(health_cli, ["--strict", *extra_args], catch_exceptions=False)

    assert result.exit_code == 1
    assert "HIGH" in result.output


@pytest.mark.unit
def test_pipeline_health_cli_json_flag_exits_0_when_all_low(tmp_path: Path) -> None:
    """--json exits 0 when all items are LOW risk."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "low.md").write_text(_LOW_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, ["--json", "--strict"], catch_exceptions=False)

    assert result.exit_code == 0, f"Expected exit 0 (all LOW), got {result.exit_code}:\n{result.output}"
    import json

    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert all(item["risk_tier"] == "LOW" for item in parsed)


@pytest.mark.unit
def test_pipeline_health_cli_compact_flag_truncates_lines(tmp_path: Path) -> None:
    """--compact flag: data rows are truncated to 80 chars; separator uses 80 dashes."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(_HIGH_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, ["--compact"], catch_exceptions=False)

    lines = result.output.splitlines()
    assert result.exit_code == 0
    # The closing separator line under --compact must be 80 dashes (not 124)
    separator_lines = [ln for ln in lines if ln and all(c == "-" for c in ln)]
    assert separator_lines, "Expected at least one separator line"
    # At least one separator should be exactly 80 (compact closing separator)
    assert any(len(ln) == 80 for ln in separator_lines), (
        f"Expected at least one 80-char separator under --compact; got: {separator_lines}"
    )


@pytest.mark.unit
def test_pipeline_health_cli_no_pursuit_files_exits_3(tmp_path: Path) -> None:
    """exits 3 when accounts/ directory exists but contains no pursuit files."""
    # accounts/ dir exists but no .md files
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True)

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, ["--strict"], catch_exceptions=False)

    assert result.exit_code == 3, f"Expected exit 3 (no pursuit files), got {result.exit_code}:\n{result.output}"


@pytest.mark.unit
def test_pipeline_health_cli_accounts_dir_missing_exits_3(tmp_path: Path) -> None:
    """exits 3 when the accounts/ directory does not exist under data root."""
    # tmp_path has NO accounts/ subdirectory
    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, [], catch_exceptions=False)

    assert result.exit_code == 3, f"Expected exit 3 (accounts dir missing), got {result.exit_code}:\n{result.output}"


@pytest.mark.unit
def test_pipeline_health_cli_all_low_exits_0(tmp_path: Path) -> None:
    """exits 0 when all active pursuits are LOW risk."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(_LOW_RISK_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(health_cli, [], catch_exceptions=False)

    assert result.exit_code == 0, f"Expected exit 0 (all LOW risk), got {result.exit_code}:\n{result.output}"
    assert "LOW" in result.output
