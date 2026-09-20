"""Regression coverage for removing the local MEDDPICC frontmatter writer."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.sf import frontmatter
from fieldkit.commands.sf.frontmatter import cli
from fieldkit.errors import FieldkitError

pytestmark = pytest.mark.unit


def test_frontmatter_help_has_workspace_dry_run_but_no_local_score_writer_options() -> None:
    """Expose workspace dry-run while omitting every former local-score option."""
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "--meddpicc" not in result.output
    assert "--dry-run" in result.output
    assert "--yes" not in result.output
    assert "native ClosePlan" not in result.output


def test_former_meddpicc_option_is_rejected_without_writing(tmp_path: Path) -> None:
    """Reject the removed score option without touching pursuit content."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\ngate-status: pending\nmeddpicc:\n  champion: 0\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")

    result = CliRunner().invoke(cli, ["--file", str(path), "--meddpicc", "champion=2"])

    assert result.exit_code != 0
    assert "No such option '--meddpicc'" in result.output
    assert path.read_text(encoding="utf-8") == original


def test_quality_check_does_not_issue_legacy_numeric_advice(tmp_path: Path) -> None:
    """Keep historical scores out of current quality-check advice."""
    path = tmp_path / "pursuit.md"
    path.write_text(
        "---\nstage: discover\ngate-status: pending\nmeddpicc:\n  champion: 0\n  composite: 0/24\n---\n\n# Body\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--quality-check", "--file", str(path)])

    assert result.exit_code == 0
    assert "MEDDPICC" not in result.output
    assert "no issues found" in result.output


def test_sf_write_canonicalizes_existing_legacy_scores(tmp_path: Path) -> None:
    """Canonicalize former scores when an independent Salesforce write is authorized."""
    path = tmp_path / "pursuit.md"
    path.write_text(
        "---\nstage: discover\ngate-status: pending\nmeddpicc:\n  champion: 7\n  composite: original\n---\n\n# Body\n",
        encoding="utf-8",
    )
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    with patch("fieldkit.commands.sf.frontmatter._validate_frontmatter_content", return_value=[]):
        result = CliRunner().invoke(cli, [str(path), payload])

    assert result.exit_code == 0, result.output
    written = path.read_text(encoding="utf-8")
    assert "legacy_meddpicc:" in written
    assert "  champion: 7" in written
    assert "  composite: original" in written
    assert "\nmeddpicc:" not in written
    assert written.endswith("---\n\n# Body\n")


@pytest.mark.parametrize("dry_run", [False, True])
def test_sf_legacy_migration_preserves_quoted_additional_key(tmp_path: Path, dry_run: bool) -> None:
    """The canonical write and its preview both validate the final safely quoted mapping."""
    path = tmp_path / "pursuit.md"
    original = (
        '---\nstage: discover\ngate-status: pending\n"custom: note": keep\nmeddpicc:\n  champion: 2\n---\n\n# Body\n'
    )
    path.write_text(original, encoding="utf-8")
    payload = '{"status":"ok","opportunity_id":"006000000000AAA","stage":"Propose"}'
    args = [str(path), payload]
    if dry_run:
        args.insert(0, "--dry-run")

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    if dry_run:
        assert path.read_text(encoding="utf-8") == original
    else:
        written = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        assert written["custom: note"] == "keep"
        assert written["legacy_meddpicc"]["champion"] == 2


@pytest.mark.parametrize("dry_run", [False, True])
def test_sf_legacy_migration_validates_canonical_document_before_write(tmp_path: Path, dry_run: bool) -> None:
    """Reject the exact canonical rendering in both preview and write modes before replacement."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\ngate-status: pending\nmeddpicc:\n  champion: 2\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    payload = '{"status":"ok","opportunity_id":"006000000000AAA","stage":"Propose"}'
    args = [str(path), payload]
    if dry_run:
        args.insert(0, "--dry-run")

    with patch(
        "fieldkit.commands.sf.frontmatter._validate_frontmatter_content",
        side_effect=[[], ["canonical rendering rejected"]],
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "canonical rendering rejected" in result.output
    assert path.read_text(encoding="utf-8") == original


def test_sf_dry_run_human_previews_legacy_migration_without_writing(tmp_path: Path) -> None:
    """Preview history migration in human output without changing the file."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\nmeddpicc:\n  champion: 7\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    original_mtime = path.stat().st_mtime_ns
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    result = CliRunner().invoke(cli, ["--dry-run", str(path), payload])

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "would update Salesforce frontmatter" in result.output
    assert "meddpicc -> legacy_meddpicc" in result.output
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == original_mtime


def test_sf_dry_run_json_reports_preview_without_writing(tmp_path: Path) -> None:
    """Report a JSON dry-run plan without changing content or modification time."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    original_mtime = path.stat().st_mtime_ns
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    result = CliRunner().invoke(cli, ["--dry-run", "--json", str(path), payload])

    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["mode"] == "sf"
    assert preview["written"] is False
    assert preview["dry_run"] is True
    assert preview["record_kind"] == "account"
    assert preview["legacy_migration"] is False
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == original_mtime


def test_sf_dry_run_json_reports_guard_refusal_without_writing(tmp_path: Path) -> None:
    """Represent a rejected payload as a non-writing JSON preview result."""
    path = tmp_path / "pursuit.md"
    original = '---\nstage: discover\nsf_opportunity_id: "006000000000AAA"\n---\n\n# Body\n'
    path.write_text(original, encoding="utf-8")
    original_mtime = path.stat().st_mtime_ns
    payload = '{"status":"ok","opportunity_id":null,"stage":"Propose"}'

    result = CliRunner().invoke(cli, ["--dry-run", "--json", str(path), payload])

    assert result.exit_code == 0, result.output
    refusal = json.loads(result.stdout)
    assert refusal["written"] is False
    assert refusal["dry_run"] is True
    assert refusal["reason"] == "payload rejected"
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == original_mtime


def test_sf_dry_run_human_reports_guard_refusal_without_writing(tmp_path: Path) -> None:
    """Show a guard refusal in human dry-run output without writing."""
    path = tmp_path / "pursuit.md"
    original = '---\nstage: discover\nsf_opportunity_id: "006000000000AAA"\n---\n\n# Body\n'
    path.write_text(original, encoding="utf-8")
    payload = '{"status":"ok","opportunity_id":null,"stage":"Propose"}'

    result = CliRunner().invoke(cli, ["--dry-run", str(path), payload])

    assert result.exit_code == 0, result.output
    assert "DRY RUN: no write" in result.output
    assert "payload rejected" in result.output
    assert path.read_text(encoding="utf-8") == original


def test_sf_dry_run_opportunity_runs_non_mutating_drift_checks(tmp_path: Path) -> None:
    """Run opportunity drift checks during dry-run while preserving the file."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    payload = '{"status":"ok","opportunity_id":"006000000000AAA","stage":"Propose"}'

    with (
        patch("fieldkit.commands.sf.frontmatter._validate_frontmatter_content", return_value=[]),
        patch("fieldkit.commands.sf.frontmatter._warn_name_slug_divergence") as warn_name,
        patch("fieldkit.commands.sf.frontmatter._warn_stage_drift", return_value=True) as warn_stage,
    ):
        result = CliRunner().invoke(cli, ["--dry-run", "--json", str(path), payload])

    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["written"] is False
    assert preview["record_kind"] == "opportunity"
    assert preview["stage_drift"] is True
    warn_name.assert_called_once()
    warn_stage.assert_called_once()
    assert path.read_text(encoding="utf-8") == original


def test_sf_dry_run_schema_failure_does_not_write(tmp_path: Path) -> None:
    """Reject an invalid dry-run rendering before any file write."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    original_mtime = path.stat().st_mtime_ns
    payload = '{"status":"ok","opportunity_id":"006000000000AAA","stage":"Propose"}'

    with patch("fieldkit.commands.sf.frontmatter._validate_frontmatter_content", return_value=["invalid preview"]):
        result = CliRunner().invoke(cli, ["--dry-run", str(path), payload])

    assert result.exit_code != 0
    assert "SCHEMA ERROR: invalid preview" in result.output
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == original_mtime


def test_sf_dry_run_validates_legacy_migration_without_writing(tmp_path: Path) -> None:
    """Validate historical canonicalization during dry-run without persisting it."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\nmeddpicc:\n  schema_version: 2\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    original_mtime = path.stat().st_mtime_ns
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    result = CliRunner().invoke(cli, ["--dry-run", str(path), payload])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "reserved schema_version" in str(result.exception)
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == original_mtime


def test_prepare_legacy_migration_rejects_unparseable_assembled_frontmatter() -> None:
    """Reject a migration whose canonical assembled document cannot be parsed."""
    with (
        patch("fieldkit.commands.sf.frontmatter.parse_frontmatter", side_effect=[({"meddpicc": {}}, ""), None]),
        pytest.raises(FieldkitError, match="Could not parse assembled frontmatter"),
    ):
        frontmatter._prepare_legacy_migration("original", "updated", "pursuit.md")


def test_quality_check_json_reports_missing_frontmatter(tmp_path: Path) -> None:
    """Represent a body-only file accurately in JSON quality-check output."""
    path = tmp_path / "pursuit.md"
    path.write_text("# Body only\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["--quality-check", "--json", "--file", str(path)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "mode": "quality-check",
        "file": str(path),
        "has_frontmatter": False,
        "advisory_count": 0,
    }


def test_quality_check_reports_file_read_error(tmp_path: Path) -> None:
    """Surface a quality-check read failure instead of silently succeeding."""
    path = tmp_path / "pursuit.md"
    path.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    with patch.object(Path, "open", side_effect=OSError("permission denied")):
        result = CliRunner().invoke(cli, ["--quality-check", "--file", str(path)])

    assert result.exit_code != 0
    assert "ERROR: Cannot read" in result.output
    assert "permission denied" in result.output


@pytest.mark.parametrize("read_flag", ["--validate", "--quality-check"])
def test_read_modes_reject_dry_run_as_ambiguous(tmp_path: Path, read_flag: str) -> None:
    """Reject dry-run for read-only modes where it has no coherent meaning."""
    path = tmp_path / "pursuit.md"
    original = "---\nstage: discover\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")

    result = CliRunner().invoke(cli, [read_flag, "--file", str(path), "--dry-run"])

    assert result.exit_code != 0
    assert "--dry-run is only valid for SF-mode" in result.output
    assert path.read_text(encoding="utf-8") == original


def test_sf_legacy_migration_preserves_unrelated_yaml_scalar_values(tmp_path: Path) -> None:
    """Preserve unrelated scalar semantics during an authorized migration."""
    path = tmp_path / "pursuit.md"
    path.write_text(
        "---\n"
        'title: "Plan # priority"\n'
        'quoted-date: "2026-09-12"\n'
        'leading-space: "  preserve"\n'
        'trailing-space: "preserve  "\n'
        "multiline: |-\n"
        "  first line\n"
        "  second # literal\n"
        "meddpicc:\n"
        "  champion: 7\n"
        "  composite: original\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    original = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    unrelated_keys = ("title", "quoted-date", "leading-space", "trailing-space", "multiline")
    original_unrelated = {key: original[key] for key in unrelated_keys}
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    result = CliRunner().invoke(cli, ["--json", str(path), payload])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["written"] is True
    written = path.read_text(encoding="utf-8")
    migrated = yaml.safe_load(written.split("---", 2)[1])
    assert {key: migrated[key] for key in unrelated_keys} == original_unrelated
    assert migrated["legacy_meddpicc"]["champion"] == 7
    assert "meddpicc" not in migrated
    assert written.endswith("---\n\n# Body\n")


@pytest.mark.parametrize(
    "value",
    ["\x00", "\x01", "\x1f", "\x7f", "\x85", "\u2028", "\u2029"],
    ids=("nul", "start-of-heading", "unit-separator", "delete", "next-line", "line-separator", "paragraph-separator"),
)
def test_sf_legacy_migration_preserves_unicode_and_control_characters(tmp_path: Path, value: str) -> None:
    """Escape YAML-sensitive characters without changing their semantic value."""
    semantic_value = f"before{value}after"
    escaped_value = semantic_value.encode("unicode_escape").decode("ascii")
    path = tmp_path / "pursuit.md"
    path.write_text(
        f'---\nunrelated-control: "{escaped_value}"\nmeddpicc:\n  champion: 7\n---\n\n# Body\n',
        encoding="utf-8",
    )
    payload = '{"status":"ok","account_id":"001000000000AAA","industry":"Technology"}'

    result = CliRunner().invoke(cli, ["--json", str(path), payload])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["written"] is True
    written = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert written["unrelated-control"] == semantic_value
    assert written["legacy_meddpicc"]["champion"] == 7
    assert "meddpicc" not in written
