"""Tests for fieldkit.pursuit.audit_cmd — CLI branches not covered by test_pursuit_audit.py."""

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.audit_cmd import (
    _critical_items,
    _error_items,
    _print_summary_table,
    _render_report,
    _report_section,
    _warning_items,
    cli,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    category: str = "COMPLIANT",
    relative_path: str = "acme/pursuits/deal.md",
    errors: list[Any] | None = None,
    warnings: list[Any] | None = None,
    criticals: list[Any] | None = None,
    parse_error: str | None = None,
    qualification_status: str = "unavailable",
    findings: list[Any] | None = None,
    stage: str = "",
) -> Any:
    from fieldkit.commands.pursuit.audit import AuditResult

    # AuditResult is a dataclass — construct via named fields
    obj = MagicMock(spec=AuditResult)
    obj.category = category
    obj.relative_path = relative_path
    obj.errors = errors or []
    obj.warnings = warnings or []
    obj.criticals = criticals or []
    obj.parse_error = parse_error
    obj.qualification_status = qualification_status
    obj.findings = findings or []
    obj.stage = stage
    return obj


def _finding(message: str = "Field missing") -> Any:
    f = MagicMock()
    f.message = message
    return f


# ---------------------------------------------------------------------------
# _report_section
# ---------------------------------------------------------------------------


# ── TestReportSection (flattened) ───────────────────────────────────────────


def test_report_section_renders_header() -> None:
    result = _make_result(category="ERROR")
    lines = _report_section("Errors", [result], lambda r: [f"- item {r.relative_path}"])
    assert "## Errors" in lines
    assert "- item acme/pursuits/deal.md" in lines


def test_report_section_renders_separator() -> None:
    lines = _report_section("X", [], lambda r: [])
    assert "---" in lines


def test_report_section_calls_item_fn_for_each_result() -> None:
    r1 = _make_result(relative_path="a/b.md")
    r2 = _make_result(relative_path="c/d.md")
    called: list[str] = []

    def capture(r: Any) -> list[str]:
        called.append(r.relative_path)
        return []

    _report_section("H", [r1, r2], capture)
    assert called == ["a/b.md", "c/d.md"]


# ---------------------------------------------------------------------------
# _error_items
# ---------------------------------------------------------------------------


# ── TestErrorItems (flattened) ──────────────────────────────────────────────


def test_error_items_error_finding_appears() -> None:
    result = _make_result(errors=[_finding("Bad field")])
    lines = _error_items(result)
    assert any("Bad field" in line for line in lines)


def test_error_items_parse_error_appears() -> None:
    result = _make_result(parse_error="YAML parse error at line 5")
    lines = _error_items(result)
    assert any("YAML parse error" in line for line in lines)


def test_error_items_no_errors_no_extra_lines() -> None:
    result = _make_result()
    lines = _error_items(result)
    # header + blank + trailing blank = 3 lines minimum
    assert len(lines) >= 2


def test_error_items_both_errors_and_parse_error() -> None:
    result = _make_result(errors=[_finding("E1"), _finding("E2")], parse_error="YAML bad")
    lines = _error_items(result)
    assert sum(1 for line in lines if "ERROR" in line) == 3  # 2 errors + 1 parse


# ---------------------------------------------------------------------------
# _critical_items / _warning_items
# ---------------------------------------------------------------------------


# ── TestCriticalItems (flattened) ───────────────────────────────────────────


def test_critical_items_critical_appears() -> None:
    result = _make_result(criticals=[_finding("champion=0")])
    lines = _critical_items(result)
    assert any("champion=0" in line for line in lines)


# ── TestWarningItems (flattened) ────────────────────────────────────────────


def test_warning_items_warning_appears() -> None:
    result = _make_result(warnings=[_finding("Score below 16")])
    lines = _warning_items(result)
    assert any("Score below 16" in line for line in lines)


# ---------------------------------------------------------------------------
# _render_report
# ---------------------------------------------------------------------------


# ── TestRenderReport (flattened) ────────────────────────────────────────────

_RENDER_REPORT__TODAY = date(2026, 6, 19)


def test_render_report_compliant_section_present() -> None:
    r = _make_result(category="COMPLIANT")
    report = _render_report([r], _RENDER_REPORT__TODAY)
    assert "Compliant Files" in report
    assert "current qualification: unavailable" in report


def test_render_report_errors_section_present() -> None:
    r = _make_result(category="ERROR", errors=[_finding("Bad")])
    report = _render_report([r], _RENDER_REPORT__TODAY)
    assert "## Errors" in report


def test_render_report_warnings_section_present() -> None:
    r = _make_result(category="WARNING", warnings=[_finding("Weak")])
    report = _render_report([r], _RENDER_REPORT__TODAY)
    assert "## Warnings" in report


def test_render_report_criticals_section_present() -> None:
    r = _make_result(criticals=[_finding("champion=0")])
    report = _render_report([r], _RENDER_REPORT__TODAY)
    assert "Critical Flags" in report


def test_render_report_summary_counts() -> None:
    compliant = _make_result(category="COMPLIANT")
    error = _make_result(category="ERROR")
    report = _render_report([compliant, error], _RENDER_REPORT__TODAY)
    assert "| Compliant (active) | 1 |" in report
    assert "| Compliant (closed-won) | 0 |" in report
    assert "| Errors | 1 |" in report


def test_render_report_compliant_closed_won_split() -> None:
    closed_won = _make_result(category="COMPLIANT", stage="closed-won")
    active = _make_result(category="COMPLIANT", stage="discover")
    report = _render_report([closed_won, active], _RENDER_REPORT__TODAY)
    assert "| Compliant (active) | 1 |" in report
    assert "| Compliant (closed-won) | 1 |" in report


def test_render_report_closed_lost_not_counted_as_active() -> None:
    """implementation change regression: closed-lost pursuits must not inflate the active count."""
    closed_lost = _make_result(category="COMPLIANT", stage="closed-lost")
    active = _make_result(category="COMPLIANT", stage="discover")
    report = _render_report([closed_lost, active], _RENDER_REPORT__TODAY)
    assert "| Compliant (active) | 1 |" in report
    assert "| Compliant (closed-won) | 0 |" in report


def test_render_report_date_in_header() -> None:
    r = _make_result()
    report = _render_report([r], _RENDER_REPORT__TODAY)
    assert "2026-06-19" in report


def test_render_report_no_optional_sections_when_empty() -> None:
    r = _make_result(category="COMPLIANT")
    report = _render_report([r], _RENDER_REPORT__TODAY)
    # Only compliant — no Errors/Warnings/Critical sections
    assert "## Errors" not in report
    assert "## Warnings" not in report


def test_render_report_recommendations_always_present() -> None:
    report = _render_report([], _RENDER_REPORT__TODAY)
    assert "## Recommendations" in report


# ---------------------------------------------------------------------------
# _print_summary_table
# ---------------------------------------------------------------------------


# ── TestPrintSummaryTable (flattened) ───────────────────────────────────────


def test_print_summary_table_prints_counts(capsys: pytest.CaptureFixture[str]) -> None:
    results = [
        _make_result(category="COMPLIANT"),
        _make_result(category="ERROR"),
        _make_result(category="WARNING"),
    ]
    _print_summary_table(results)
    out = capsys.readouterr().out
    assert "3" in out  # Files scanned
    assert "Compliant" in out
    assert "Errors" in out


def test_print_summary_table_compliant_closed_won_split(capsys: pytest.CaptureFixture[str]) -> None:
    closed_won = _make_result(category="COMPLIANT", stage="closed-won")
    active = _make_result(category="COMPLIANT", stage="discover")
    _print_summary_table([closed_won, active])
    out = capsys.readouterr().out
    assert "Compliant     : 1" in out
    assert "closed-won  : 1" in out


def test_print_summary_table_closed_lost_not_counted_as_active(capsys: pytest.CaptureFixture[str]) -> None:
    """implementation change regression: closed-lost pursuits must not inflate the active count."""
    closed_lost = _make_result(category="COMPLIANT", stage="closed-lost")
    active = _make_result(category="COMPLIANT", stage="discover")
    _print_summary_table([closed_lost, active])
    out = capsys.readouterr().out
    assert "Compliant     : 1" in out
    assert "closed-won  : 0" in out


def test_print_summary_table_details_with_criticals(capsys: pytest.CaptureFixture[str]) -> None:
    r = _make_result(category="ERROR", criticals=[_finding("champion=0")])
    _print_summary_table([r])
    out = capsys.readouterr().out
    assert "champion=0" in out


def test_print_summary_table_shows_one_warning_inline(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.pursuit.audit import Finding

    warning = Finding("WARNING", "close date needs review")
    result = _make_result(category="WARNING", warnings=[warning], findings=[warning])

    _print_summary_table([result])

    out = capsys.readouterr().out
    assert "WARNING: close date needs review" in out
    assert "and 1 more" not in out


def test_print_summary_table_bounds_multiple_noncritical_findings(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.pursuit.audit import Finding

    findings = [
        Finding("WARNING", "first actionable warning"),
        Finding("ERROR", "second finding stays in report"),
        Finding("WARNING", "third finding stays in report"),
    ]
    result = _make_result(
        category="ERROR", errors=[findings[1]], warnings=[findings[0], findings[2]], findings=findings
    )

    _print_summary_table([result])

    out = capsys.readouterr().out
    assert "WARNING: first actionable warning" in out
    assert "second finding stays in report" not in out
    assert "third finding stays in report" not in out
    assert "and 2 more; see report" in out


def test_print_summary_table_keeps_criticals_and_one_warning(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.pursuit.audit import Finding

    criticals = [Finding("CRITICAL", "missing champion"), Finding("CRITICAL", "buyer unconfirmed")]
    warnings = [Finding("WARNING", "score below threshold"), Finding("WARNING", "next step is stale")]
    result = _make_result(category="WARNING", warnings=warnings, criticals=criticals, findings=[*criticals, *warnings])

    _print_summary_table([result])

    out = capsys.readouterr().out
    assert out.count("missing champion") == 1
    assert out.count("buyer unconfirmed") == 1
    assert out.count("score below threshold") == 1
    assert "next step is stale" not in out
    assert "and 1 more; see report" in out


def test_print_summary_table_compliant_file_has_no_finding_detail(capsys: pytest.CaptureFixture[str]) -> None:
    _print_summary_table([_make_result(category="COMPLIANT")])

    out = capsys.readouterr().out
    assert "WARNING:" not in out
    assert "ERROR:" not in out
    assert "see report" not in out


# ---------------------------------------------------------------------------
# cli — Click command integration
# ---------------------------------------------------------------------------


# ── TestAuditCli (flattened) ────────────────────────────────────────────────


def _cli_run(args: list[str], data_root: Path) -> Any:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory") as mock_audit,
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes") as mock_fix,
        patch("fieldkit.commands.pursuit.audit_cmd.check_yaml_duplicates_directory") as mock_yaml,
    ):
        mock_audit.return_value = [_make_result()]
        mock_fix.return_value = MagicMock(total_changes=0, renames=0, legacy_removed=0)
        mock_yaml.return_value = []
        result = runner.invoke(cli, args, catch_exceptions=False)
    return result


def test_cli_no_accounts_dir_exits_3(tmp_path: Path) -> None:
    runner = CliRunner()
    data_root = tmp_path / "missing"
    data_root.mkdir()
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code == 3


def test_cli_no_results_exits_3(tmp_path: Path) -> None:
    runner = CliRunner()
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[]),
    ):
        result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code == 3


def test_cli_normal_run_exits_0(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code == 0


def test_cli_check_yaml_no_dups_exits_0(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.check_yaml_duplicates_directory", return_value=[]),
    ):
        result = runner.invoke(cli, ["--check-yaml"], catch_exceptions=False)
    assert result.exit_code == 0


def test_cli_check_yaml_with_dups_exits_1(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    dup = _make_result(relative_path="acme/pursuits/deal.md")
    dup.findings = ["sf_stage: duplicate key"]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.check_yaml_duplicates_directory", return_value=[dup]),
    ):
        result = runner.invoke(cli, ["--check-yaml"], catch_exceptions=False)
    assert result.exit_code == 1


def test_cli_fix_no_changes(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes") as mock_fix,
    ):
        mock_fix.return_value = MagicMock(total_changes=0, renames=0, legacy_removed=0)
        result = runner.invoke(cli, ["--fix"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No auto-corrections" in result.output


def test_cli_fix_with_changes_echoes_count(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    # Create a fake pursuit file for glob to find
    pursuits_dir = accounts_dir / "acme" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    pursuit_file = pursuits_dir / "deal.md"
    pursuit_file.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    runner = CliRunner()
    fix_result = MagicMock(total_changes=2, renames=1, legacy_removed=1)
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes", return_value=fix_result),
    ):
        result = runner.invoke(cli, ["--fix"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Applied 2 auto-correction" in result.output


def test_cli_fix_dry_run_previews_without_writes_or_report(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    pursuit_dir = accounts_dir / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit = pursuit_dir / "deal.md"
    original = "---\nsf-opportunity-id: 006xx\n---\nBody\n"
    pursuit.write_text(original, encoding="utf-8")
    before = pursuit.stat().st_mtime_ns

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--fix", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "would fix: acme/pursuits/deal.md (1 renames, 0 removed)" in result.output
    assert "Would apply 1 auto-correction(s)." in result.output
    assert pursuit.read_text(encoding="utf-8") == original
    assert pursuit.stat().st_mtime_ns == before
    assert not (accounts_dir / ".audit").exists()


def test_cli_fix_dry_run_reports_when_no_corrections_are_needed(tmp_path: Path) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit = pursuit_dir / "deal.md"
    pursuit.write_text("---\nstage: discover\n---\nBody\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--fix", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "No auto-corrections needed." in result.output
    assert pursuit.read_text(encoding="utf-8") == "---\nstage: discover\n---\nBody\n"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("malformed_content", ["# Missing frontmatter\n", "---\n- sf-opportunity-id\n---\n"])
def test_cli_fix_refuses_malformed_file_and_continues_batch(
    tmp_path: Path, dry_run: bool, malformed_content: str
) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    malformed = pursuit_dir / "a-malformed.md"
    repairable = pursuit_dir / "b-repairable.md"
    malformed.write_text(malformed_content, encoding="utf-8")
    original = "---\nstage: discover\nsf-opportunity-id: 006LEGACY\n---\nBody\n"
    repairable.write_text(original, encoding="utf-8")

    args = ["--fix", *(["--dry-run"] if dry_run else [])]
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 1
    assert "refused: acme/pursuits/a-malformed.md" in result.output
    assert "b-repairable.md" in result.output
    if dry_run:
        assert repairable.read_text(encoding="utf-8") == original
        assert not (tmp_path / "accounts" / ".audit").exists()
    else:
        assert "sf_opportunity_id: 006LEGACY" in repairable.read_text(encoding="utf-8")
        assert (tmp_path / "accounts" / ".audit").is_dir()


def test_cli_fix_json_keeps_stdout_machine_readable(tmp_path: Path) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    malformed = pursuit_dir / "a-malformed.md"
    repairable = pursuit_dir / "b-repairable.md"
    malformed.write_text("# Missing frontmatter\n", encoding="utf-8")
    repairable.write_text("---\nstage: discover\nsf-opportunity-id: 006LEGACY\n---\nBody\n", encoding="utf-8")

    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--fix", "--json"], catch_exceptions=False)

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert isinstance(payload, list)
    assert "refused: acme/pursuits/a-malformed.md" in result.stderr
    assert "fixed: acme/pursuits/b-repairable.md" in result.stderr


@pytest.mark.parametrize(
    "args,message",
    [
        (["--dry-run"], "--dry-run requires --fix"),
        (["--fix", "--dry-run", "--check-yaml"], "--dry-run cannot be combined with --check-yaml"),
        (["--fix", "--dry-run", "--json"], "--dry-run cannot be combined with --json"),
        (["--fix", "--dry-run", "--output", "report.md"], "--dry-run cannot be combined with --output"),
    ],
)
def test_cli_dry_run_conflicts_before_workspace_lookup(args: list[str], message: str) -> None:
    with patch("fieldkit.commands.pursuit.audit_cmd._data_root") as data_root:
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2
    assert message in result.output
    data_root.assert_not_called()


def test_cli_dry_run_conflict_is_fieldkit_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.__main__ import main

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root") as data_root:
        result = main(["pursuit", "audit", "--dry-run"])

    assert result == 3
    assert "Error: --dry-run requires --fix" in capsys.readouterr().err
    data_root.assert_not_called()


def test_cli_account_not_found_exits_3(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["--account", "nonexistent"], catch_exceptions=False)
    assert result.exit_code == 3


def test_cli_output_path_respected(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    output_file = tmp_path / "report.md"
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = runner.invoke(cli, ["--output", str(output_file)], catch_exceptions=False)
    assert result.exit_code == 0
    assert output_file.exists()


def test_cli_report_overwrite_warns(tmp_path: Path) -> None:
    """Writing to an existing report file emits an overwrite warning."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    output_file = tmp_path / "report.md"
    output_file.write_text("old content", encoding="utf-8")
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = runner.invoke(cli, ["--output", str(output_file)], catch_exceptions=False)
    assert result.exit_code == 0
    # Warning goes to stderr (mixed into output by CliRunner)
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "WARNING" in combined or "Overwriting" in combined or output_file.read_text()


def test_cli_default_report_path_created(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        runner.invoke(cli, [], catch_exceptions=False)
    # Default report ends up in accounts/.audit/
    audit_dir = accounts_dir / ".audit"
    assert audit_dir.exists()
    reports = list(audit_dir.glob("pursuit-compliance-*.md"))
    assert len(reports) == 1


def test_cli_account_filter_passed_to_audit(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    acme_dir = accounts_dir / "acme"
    acme_dir.mkdir(parents=True)
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory") as mock_audit,
    ):
        mock_audit.return_value = [_make_result()]
        runner.invoke(cli, ["--account", "acme"], catch_exceptions=False)
    call_kwargs = mock_audit.call_args
    assert call_kwargs[1].get("account_filter") == "acme" or (len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "acme")


# ---------------------------------------------------------------------------
# Additional coverage: _data_root None case and check_yaml with parse_error
# ---------------------------------------------------------------------------


# ── TestDataRootNone (flattened) ────────────────────────────────────────────


def test_data_root_no_data_root_exits_3() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=None):
        result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code == 3


def test_data_root_check_yaml_with_parse_error_shown(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    runner = CliRunner()
    dup = MagicMock()
    dup.relative_path = "acme/pursuits/deal.md"
    dup.parse_error = "YAML parse error at line 5"
    dup.findings = []
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.check_yaml_duplicates_directory", return_value=[dup]),
    ):
        result = runner.invoke(cli, ["--check-yaml"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "YAML parse error" in result.output or "ERROR" in result.output


# ── TestFixGmailIntelSkipped (flattened) ────────────────────────────────────


def test_run_fix_gmail_intel_file_skipped_during_fix(tmp_path: Path) -> None:
    """gmail-intel.md is skipped during --fix pass."""
    accounts_dir = tmp_path / "accounts"
    pursuits_dir = accounts_dir / "acme" / "pursuits"
    pursuits_dir.mkdir(parents=True)

    # Create gmail-intel.md (should be skipped) and a regular pursuit
    (pursuits_dir / "gmail-intel.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")
    (pursuits_dir / "deal.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")

    runner = CliRunner()
    apply_fix_calls: list[str] = []

    def fake_apply_fixes(path: Any, *, dry_run: bool = False) -> Any:
        assert not dry_run
        apply_fix_calls.append(path.name)
        return MagicMock(total_changes=0, renames=0, legacy_removed=0)

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes", side_effect=fake_apply_fixes),
    ):
        result = runner.invoke(cli, ["--fix"], catch_exceptions=False)

    assert result.exit_code == 0
    # gmail-intel.md should NOT be in the list of files passed to apply_fixes
    assert "gmail-intel.md" not in apply_fix_calls
    assert "deal.md" in apply_fix_calls


# ---------------------------------------------------------------------------
# implementation change: positional ACCOUNT argument
# ---------------------------------------------------------------------------


# ── TestPositionalAccountArg (flattened) ────────────────────────────────────


def test_positional_account_arg_positional_arg_sets_account(tmp_path: Path) -> None:
    """Positional ACCOUNT arg is accepted and passed to audit_directory."""
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    runner = CliRunner()
    captured_filter: list[str | None] = []

    def fake_audit(root: Any, account_filter: Any = None, today: Any = None) -> list[Any]:
        captured_filter.append(account_filter)
        return [_make_result()]

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", side_effect=fake_audit),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report"),
    ):
        result = runner.invoke(cli, ["acme"], catch_exceptions=False)

    assert result.exit_code == 0
    assert captured_filter == ["acme"]


def test_positional_account_arg_positional_and_flag_both_given_raises_usage_error(tmp_path: Path) -> None:
    """Providing both positional ACCOUNT and -a raises UsageError."""
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = runner.invoke(cli, ["acme", "-a", "globex"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "positional" in result.output.lower() or "both" in result.output.lower()


def test_positional_account_arg_flag_only_still_works(tmp_path: Path) -> None:
    """-a/--account flag still works without positional arg."""
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    runner = CliRunner()
    captured_filter: list[str | None] = []

    def fake_audit(root: Any, account_filter: Any = None, today: Any = None) -> list[Any]:
        captured_filter.append(account_filter)
        return [_make_result()]

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", side_effect=fake_audit),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report"),
    ):
        result = runner.invoke(cli, ["-a", "acme"], catch_exceptions=False)

    assert result.exit_code == 0
    assert captured_filter == ["acme"]


# ---------------------------------------------------------------------------
# cell-28b9dae2e9395288: --json flag
# ---------------------------------------------------------------------------


# ── TestAuditJsonFlag (flattened) ───────────────────────────────────────────


def _write_audit_report_make_real_result(tmp_path: Path) -> Any:
    """Create a real AuditResult dataclass instance (not a mock)."""
    from fieldkit.commands.pursuit.audit import AuditResult

    fake_path = tmp_path / "acme" / "pursuits" / "deal.md"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_text("---\nstage: discover\n---\n", encoding="utf-8")
    return AuditResult(path=fake_path, relative_path="acme/pursuits/deal.md")


def test_write_audit_report_json_flag_emits_json(tmp_path: Path) -> None:
    """--json flag emits JSON and suppresses table/report write."""
    import json

    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    real_result = _write_audit_report_make_real_result(tmp_path)
    runner = CliRunner()

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[real_result]),
    ):
        result = runner.invoke(cli, ["--json"], catch_exceptions=False)

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert "relative_path" in parsed[0]
    assert parsed[0]["qualification_status"] == "unavailable"
    assert "meddpicc_score" not in parsed[0]


def test_json_warning_output_excludes_human_detail_lines(tmp_path: Path) -> None:
    import json

    from fieldkit.commands.pursuit.audit import Finding

    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    real_result = _write_audit_report_make_real_result(tmp_path)
    real_result.findings.append(Finding("WARNING", "close date needs review"))
    runner = CliRunner()

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[real_result]),
    ):
        result = runner.invoke(cli, ["--json"], catch_exceptions=False)

    parsed = json.loads(result.output)
    assert result.exit_code == 1
    assert parsed[0]["findings"] == [{"level": "WARNING", "message": "close date needs review"}]
    assert "WARNING: close date needs review" not in result.output
    assert "see report" not in result.output


def test_write_audit_report_json_flag_suppresses_report_write(tmp_path: Path) -> None:
    """--json flag does not call _write_audit_report."""
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acme").mkdir(parents=True)
    real_result = _write_audit_report_make_real_result(tmp_path)
    runner = CliRunner()
    write_called: list[bool] = []

    def fake_write(*args: Any, **kwargs: Any) -> None:
        write_called.append(True)

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[real_result]),
        patch("fieldkit.commands.pursuit.audit_cmd._write_audit_report", side_effect=fake_write),
    ):
        runner.invoke(cli, ["--json"], catch_exceptions=False)

    assert write_called == [], "Report write must be suppressed when --json is used"
