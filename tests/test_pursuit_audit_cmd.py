"""Tests for fieldkit.pursuit.audit_cmd — CLI branches not covered by test_pursuit_audit.py."""

import json
import os
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
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.util.atomic import PathLockTimeoutError

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


def test_cli_fix_dry_run_creates_no_workspace_or_runtime_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    pursuit = pursuits / "deal.md"
    pursuit.write_text("---\nsf-opportunity-id: 006LEGACY\n---\nBody\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(runtime))

    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--fix", "--dry-run"], catch_exceptions=False)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert result.exit_code == 1
    assert after == before
    assert not runtime.exists()


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


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_fix_duplicate_key_refusal_matches_preview_and_apply(tmp_path: Path, dry_run: bool) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    duplicate = pursuits / "a-duplicate.md"
    duplicate_content = "---\nsf-opportunity-id: 006A\nsf-opportunity-id: 006B\n---\nBody\n"
    duplicate.write_text(duplicate_content, encoding="utf-8")
    repairable = pursuits / "b-repairable.md"
    repairable_content = "---\nsf-opportunity-id: 006C\n---\nBody\n"
    repairable.write_text(repairable_content, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--fix", *(["--dry-run"] if dry_run else [])], catch_exceptions=False)

    assert result.exit_code == 1
    assert "repair refused: acme/pursuits/a-duplicate.md (unsafe or invalid pursuit data)" in result.output
    assert "b-repairable.md" in result.output
    assert duplicate.read_text(encoding="utf-8") == duplicate_content
    if dry_run:
        assert repairable.read_text(encoding="utf-8") == repairable_content
    else:
        assert "sf_opportunity_id: 006C" in repairable.read_text(encoding="utf-8")


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


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("private repair sentinel"),
        OSError("/fictional-private/operator/private-write-sentinel"),
        FrontmatterStalenessError("/fictional-private/operator/private-stale-sentinel"),
        PathLockTimeoutError("/fictional-private/operator/private-lock-sentinel"),
    ],
)
def test_cli_fix_failure_remains_non_success_after_compliant_reaudit(
    tmp_path: Path,
    as_json: bool,
    failure: Exception,
) -> None:
    """A later clean audit must not erase a refused or failed requested repair."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")

    from fieldkit.commands.pursuit.audit import AuditResult

    audit_result = AuditResult(path=pursuit_dir / "deal.md", relative_path="acme/pursuits/deal.md")
    args = ["--fix", *(["--json"] if as_json else [])]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes", side_effect=failure),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[audit_result]),
    ):
        result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 1
    diagnostic = result.stderr if as_json else result.output
    assert "repair" in diagnostic.lower()
    assert str(failure) not in diagnostic
    if as_json:
        assert isinstance(json.loads(result.stdout), list)


def test_cli_fix_continues_after_stale_file_and_keeps_json_valid(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    stale = pursuits / "a-stale.md"
    repairable = pursuits / "b-repairable.md"
    for path in (stale, repairable):
        path.write_text("---\nsf-opportunity-id: 006LEGACY\n---\n", encoding="utf-8")
    from fieldkit.commands.pursuit.audit import AuditResult, FixResult

    audit_results = [
        AuditResult(path=stale, relative_path="acme/pursuits/a-stale.md"),
        AuditResult(path=repairable, relative_path="acme/pursuits/b-repairable.md"),
    ]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.commands.pursuit.audit_cmd.apply_fixes",
            side_effect=[FrontmatterStalenessError("private stale path"), FixResult(path=repairable, renames=1)],
        ) as apply,
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=audit_results),
    ):
        result = CliRunner().invoke(cli, ["--fix", "--json"], catch_exceptions=False)

    assert result.exit_code == 1
    assert apply.call_count == 2
    assert isinstance(json.loads(result.stdout), list)
    assert "changed during repair" in result.stderr
    assert "private stale path" not in result.stderr


def test_cli_fix_same_mtime_replacement_preserves_bytes_and_continues_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    stale = pursuits / "a-stale.md"
    repairable = pursuits / "b-repairable.md"
    original = "---\nstage: discover\nsf-opportunity-id: 006OLD\n---\nOriginal body\n"
    concurrent = "---\nstage: discover\nsf-opportunity-id: 006NEW\n---\nConcurrent body\n"
    stale.write_text(original, encoding="utf-8")
    repairable.write_text(original, encoding="utf-8")
    stale_info = stale.stat()
    from fieldkit.commands.pursuit import audit as audit_module
    from fieldkit.commands.pursuit.audit import AuditResult

    original_render = audit_module.__dict__["render_frontmatter_raw"]

    def render_then_replace(path: Path, *args: object, **kwargs: object) -> str:
        rendered: str = original_render(path, *args, **kwargs)
        if path == stale:
            stale.write_text(concurrent, encoding="utf-8")
            os.utime(stale, ns=(stale_info.st_atime_ns, stale_info.st_mtime_ns))
        return rendered

    monkeypatch.setattr(audit_module, "render_frontmatter_raw", render_then_replace)
    audit_results = [
        AuditResult(path=stale, relative_path="acme/pursuits/a-stale.md"),
        AuditResult(path=repairable, relative_path="acme/pursuits/b-repairable.md"),
    ]
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=audit_results),
    ):
        result = CliRunner().invoke(cli, ["--fix", "--json"], catch_exceptions=False)

    assert result.exit_code == 1
    assert isinstance(json.loads(result.stdout), list)
    assert stale.read_text(encoding="utf-8") == concurrent
    assert "sf_opportunity_id: 006OLD" in repairable.read_text(encoding="utf-8")
    assert "changed during repair" in result.stderr
    assert "006NEW" not in result.stderr
    assert "Concurrent body" not in result.stderr


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


def test_cli_rejects_account_traversal_before_repairs(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    outside = tmp_path / "outside" / "pursuits"
    outside.mkdir(parents=True)
    target = outside / "deal.md"
    target.write_text("---\nsf-opportunity-id: 006LEGACY\n---\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes") as mock_fix,
    ):
        result = CliRunner().invoke(cli, ["--account", "../outside", "--fix"], catch_exceptions=False)

    assert result.exit_code == 3
    mock_fix.assert_not_called()
    assert "sf-opportunity-id" in target.read_text(encoding="utf-8")


def test_cli_rejects_symlinked_account_before_repairs(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    outside = tmp_path / "outside" / "pursuits"
    outside.mkdir(parents=True)
    target = outside / "deal.md"
    target.write_text("---\nsf-opportunity-id: 006LEGACY\n---\n", encoding="utf-8")
    (accounts_dir / "acme").symlink_to(outside.parent, target_is_directory=True)

    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.apply_fixes") as mock_fix,
    ):
        result = CliRunner().invoke(cli, ["--account", "acme", "--fix"], catch_exceptions=False)

    assert result.exit_code == 3
    mock_fix.assert_not_called()
    assert "sf-opportunity-id" in target.read_text(encoding="utf-8")


def test_cli_output_path_respected(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    audit_dir = accounts_dir / ".audit"
    audit_dir.mkdir()
    output_file = audit_dir / "report.md"
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = runner.invoke(cli, ["--output", str(output_file)], catch_exceptions=False)
    assert result.exit_code == 0
    assert output_file.exists()


def test_cli_output_path_outside_audit_directory_is_rejected(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    output_file = tmp_path / "report.md"
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--output", str(output_file)], catch_exceptions=False)

    assert result.exit_code == 3
    assert not output_file.exists()


def test_cli_output_symlink_escape_is_rejected(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    audit_dir = accounts_dir / ".audit"
    audit_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("preserve me", encoding="utf-8")
    output_file = audit_dir / "report.md"
    output_file.symlink_to(outside)
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--output", str(output_file)], catch_exceptions=False)

    assert result.exit_code == 3
    assert outside.read_text(encoding="utf-8") == "preserve me"


def test_cli_report_uses_atomic_text_replacement(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    audit_dir = accounts_dir / ".audit"
    audit_dir.mkdir(parents=True)
    output_file = audit_dir / "report.md"
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
        patch("fieldkit.commands.pursuit.audit_cmd.atomic_text_write") as mock_atomic_write,
    ):
        result = CliRunner().invoke(cli, ["--output", str(output_file)], catch_exceptions=False)

    assert result.exit_code == 0
    written_path, rendered = mock_atomic_write.call_args.args
    assert written_path == output_file
    assert rendered.startswith("# Pursuit Compliance Report")


def test_cli_report_is_private_utf8_atomic_output(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts"
    audit_dir = accounts / ".audit"
    audit_dir.mkdir(parents=True)
    output = audit_dir / "report.md"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--output", "report.md"], catch_exceptions=False)

    assert result.exit_code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.read_bytes().decode("utf-8").startswith("# Pursuit Compliance Report")


def test_cli_nested_missing_output_parent_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--output", "missing/report.md"], catch_exceptions=False)
    assert result.exit_code == 3
    assert not (tmp_path / "accounts" / ".audit" / "missing").exists()


def test_cli_report_overwrite_replaces_complete_content(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    audit_dir = accounts_dir / ".audit"
    audit_dir.mkdir()
    output_file = audit_dir / "report.md"
    output_file.write_text("old content", encoding="utf-8")
    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = runner.invoke(cli, ["--output", str(output_file)], catch_exceptions=False)
    assert result.exit_code == 0
    assert output_file.read_text(encoding="utf-8").startswith("# Pursuit Compliance Report")
    assert "old content" not in output_file.read_text(encoding="utf-8")


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


@pytest.mark.parametrize("account", ["*", "?", "[acme]"])
def test_cli_rejects_glob_account_names(tmp_path: Path, account: str) -> None:
    (tmp_path / "accounts").mkdir()
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--account", account], catch_exceptions=False)
    assert result.exit_code == 3
    assert account not in result.output


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


def test_run_fix_template_file_is_skipped(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    template = pursuits / "template.md"
    template.write_text("---\nsf-opportunity-id: 006LEGACY\n---\n", encoding="utf-8")
    deal = pursuits / "deal.md"
    deal.write_text(
        "---\nstage: discover\ngate-status: pending\nlast-transition: 2026-01-01\ntransition-history: []\n---\n",
        encoding="utf-8",
    )
    with (
        patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.audit_cmd.audit_directory", return_value=[_make_result()]),
    ):
        result = CliRunner().invoke(cli, ["--fix"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "sf-opportunity-id" in template.read_text(encoding="utf-8")


def test_cli_rejects_fix_with_check_yaml_before_workspace_access() -> None:
    with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home") as get_home:
        result = CliRunner().invoke(cli, ["--fix", "--check-yaml"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output
    get_home.assert_not_called()


def test_cli_fix_with_check_yaml_is_fieldkit_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.__main__ import main

    result = main(["pursuit", "audit", "--fix", "--check-yaml"])

    assert result == 3
    assert "--fix cannot be combined with --check-yaml" in capsys.readouterr().err


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
    assert "path" not in parsed[0]
    assert str(tmp_path) not in result.output
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
