"""Tests for pursuit exit codes and UX fixes — historic regression, historic regression, historic regression, historic regression, implementation change."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# historic regression: Exit codes
# ---------------------------------------------------------------------------


# ── TestAuditExitCodes (flattened) ──────────────────────────────────────────


def _audit_exit_codes_make_result(category: str, criticals: int = 0) -> MagicMock:
    r = MagicMock()
    r.category = category
    r.criticals = [MagicMock(message=f"c{i}") for i in range(criticals)]
    r.relative_path = "account/pursuit.md"
    r.qualification_status = "unavailable"
    r.findings = []
    return r


def test_audit_exit_codes_exits_1_with_criticals(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.audit_cmd import cli

    results = [_audit_exit_codes_make_result("ERROR", criticals=2)]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=results),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report"),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 1


def test_audit_exit_codes_exits_1_with_warnings(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.audit_cmd import cli

    results = [_audit_exit_codes_make_result("WARNING")]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=results),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report"),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 1


def test_audit_exit_codes_exits_0_all_compliant(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.audit_cmd import cli

    results = [_audit_exit_codes_make_result("COMPLIANT")]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=results),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report"),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0


# ── TestHealthExitCodes (flattened) ─────────────────────────────────────────


def _health_exit_codes_make_item(tier: str) -> MagicMock:
    item = MagicMock()
    item.risk_tier = tier
    item.relative_path = "account/pursuits/deal.md"
    item.stage = "propose"
    item.days_in_stage = 10
    item.days_until_close = 30
    item.close_date_str = "2026-09-01"
    item.qualification_status = "unavailable"
    item.risk_reasons = ["reason"]
    return item


def test_health_exit_codes_strict_exits_1_with_high_risk(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.pipeline_health import cli

    items = [_health_exit_codes_make_item("HIGH"), _health_exit_codes_make_item("LOW")]
    with (
        patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.pipeline_health.health_check", return_value=items),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, ["--strict"])
    assert result.exit_code == 1


def test_health_exit_codes_exits_0_no_high_risk(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.pipeline_health import cli

    items = [_health_exit_codes_make_item("MEDIUM"), _health_exit_codes_make_item("LOW")]
    with (
        patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.pipeline_health.health_check", return_value=items),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0


# ── TestProjectsExitCodes (flattened) ───────────────────────────────────────


def _projects_exit_codes_make_row(health: str) -> MagicMock:
    row = MagicMock()
    row.health = health
    row.name = "Project Alpha"
    row.sf_stage = "active"
    row.contract_end_str = "2026-12-31"
    row.days_until_end = 90
    return row


def test_projects_exit_codes_strict_exits_1_with_zombie(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.projects_health import cli

    rows = [_projects_exit_codes_make_row("ZOMBIE"), _projects_exit_codes_make_row("ACTIVE")]
    with (
        patch("fieldkit.commands.pursuit.projects_health.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.projects_health.health_check", return_value=rows),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, ["--strict"])
    assert result.exit_code == 1


def test_projects_exit_codes_exits_0_no_zombie(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.projects_health import cli

    rows = [_projects_exit_codes_make_row("ACTIVE"), _projects_exit_codes_make_row("EXPIRING")]
    with (
        patch("fieldkit.commands.pursuit.projects_health.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.projects_health.health_check", return_value=rows),
    ):
        (tmp_path / "accounts").mkdir()
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# historic regression: relative_path in forecast JSON
# ---------------------------------------------------------------------------


# ── TestForecastRelativePath (flattened) ────────────────────────────────────


def test_forecast_relative_path_relative_path_not_absolute(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.forecast import _parse_deal_row

    accounts_dir = tmp_path / "accounts"
    acme_dir = accounts_dir / "acme-corp" / "pursuits"
    acme_dir.mkdir(parents=True)
    path = acme_dir / "deal.md"
    path.write_text(
        "---\nstage: propose\nsf_consulting_acv: 50000\nsf_close_date: '2026-09-01'\n---\nBody\n",
        encoding="utf-8",
    )

    row = _parse_deal_row(path, accounts_dir, skipped=[])

    assert row is not None
    assert not row.relative_path.startswith("/"), f"relative_path should not start with '/', got: {row.relative_path!r}"
    assert "acme-corp" in row.relative_path


# ---------------------------------------------------------------------------
# historic regression: quota exclusion warning
# ---------------------------------------------------------------------------


# ── TestQuotaExclusionWarning (flattened) ───────────────────────────────────


def test_quota_exclusion_warning_excluded_pursuits_warned() -> None:
    from fieldkit.watch.morning_brief_render import calculate_quota_gap

    pursuits = [
        {"stage": "propose", "sf_amount": None, "sf_probability": None, "name": "no-acv-deal"},
        {"stage": "negotiate", "sf_amount": 100000, "sf_probability": None, "name": "has-acv-deal"},
    ]
    result = calculate_quota_gap(pursuits, {"target": 500000})
    assert result["excluded_count"] == 1
    assert "no-acv-deal" in result["excluded_names"]


def test_quota_exclusion_warning_no_exclusions_when_all_have_acv() -> None:
    from fieldkit.watch.morning_brief_render import calculate_quota_gap

    pursuits = [
        {"stage": "propose", "sf_amount": 100000, "sf_probability": None, "name": "deal-a"},
        {"stage": "negotiate", "sf_amount": 200000, "sf_probability": None, "name": "deal-b"},
    ]
    result = calculate_quota_gap(pursuits, {"target": 500000})
    assert result["excluded_count"] == 0
    assert result["excluded_names"] == []


# ---------------------------------------------------------------------------
# implementation change: advance --dry-run exits 1 when policy is pending
# ---------------------------------------------------------------------------


# ── TestAdvanceDryRunExitCode (flattened) ───────────────────────────────────


def _advance_dry_run_exit_code_make_pursuit(tmp_path: Path, stage: str = "qualify") -> Path:
    """Create a minimal pursuit file with the given stage."""
    f = tmp_path / "deal.md"
    f.write_text(
        f"---\nstage: {stage}\nmeddpicc: {{}}\n---\nBody\n",
        encoding="utf-8",
    )
    return f


def test_advance_dry_run_exit_code_dry_run_exits_1_on_gate_pending(tmp_path: Path) -> None:
    """When policy is pending in --dry-run, exit code is 1."""
    from fieldkit.commands.pursuit.advance_cmd import cli

    pursuit_file = _advance_dry_run_exit_code_make_pursuit(tmp_path, stage="qualify")

    with (
        patch("fieldkit.commands.pursuit.advance_cmd._resolve_pursuit_spec", return_value=pursuit_file),
        patch(
            "fieldkit.commands.pursuit.advance_cmd._evaluate_gate",
            return_value=(False, "pending", "Native policy pending", ("Native policy pending",)),
        ),
    ):
        result = CliRunner().invoke(cli, [str(pursuit_file), "--dry-run"])

    assert result.exit_code == 1, f"Expected exit 1 on gate pending, got {result.exit_code}. Output:\n{result.output}"


def test_advance_dry_run_exit_code_dry_run_exits_0_on_gate_pass(tmp_path: Path) -> None:
    """When gate passes in --dry-run, exit code should be 0."""
    from fieldkit.commands.pursuit.advance_cmd import cli

    pursuit_file = _advance_dry_run_exit_code_make_pursuit(tmp_path, stage="qualify")

    with (
        patch("fieldkit.commands.pursuit.advance_cmd._resolve_pursuit_spec", return_value=pursuit_file),
        patch(
            "fieldkit.commands.pursuit.advance_cmd._evaluate_gate",
            return_value=(True, "pass", "", ()),
        ),
    ):
        result = CliRunner().invoke(cli, [str(pursuit_file), "--dry-run"])

    assert result.exit_code == 0
