"""CLI-adapter tests for Salesforce pursuit frontmatter operations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf import frontmatter
from fieldkit.commands.sf.frontmatter import cli

pytestmark = pytest.mark.unit


def _pursuit(workspace: Path, *, backstory: bool = False) -> Path:
    path = workspace / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True)
    marker = "\nsource: '[Backstory] cached claim'" if backstory else ""
    path.write_text(
        "---\n"
        "stage: discover\n"
        "gate-status: pending\n"
        'sf_opportunity_id: "006000000000AAA"'
        f"{marker}\n"
        "---\n\n# Expansion\n",
        encoding="utf-8",
    )
    return path


def _payload() -> str:
    return json.dumps(
        {
            "status": "ok",
            "opportunity_id": "006000000000AAA",
            "name": "Expansion",
            "stage": "Propose",
            "close_date": "2027-03-31",
        }
    )


def test_sf_mode_writes_through_domain_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _pursuit(tmp_path)
    monkeypatch.setattr(frontmatter, "get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(cli, [str(path), _payload()])

    assert result.exit_code == 0
    assert "Frontmatter updated" in result.stderr
    written = path.read_text(encoding="utf-8")
    assert "sf_stage: Propose" in written
    assert 'sf_close_date: "2027-03-31"' in written
    assert "# Expansion" in written


def test_sf_dry_run_json_is_deterministic_and_write_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _pursuit(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(frontmatter, "get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(cli, [str(path), _payload(), "--dry-run", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["written"] is False
    assert document["dry_run"] is True
    assert document["record_kind"] == "opportunity"
    assert document["keys_previewed"][0] == "sf_opportunity_id"
    assert path.read_bytes() == original


def test_sf_mode_rejects_unconfined_file_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _pursuit(tmp_path / "outside")
    original = outside.read_bytes()
    monkeypatch.setattr(frontmatter, "get_fieldkit_home", lambda: workspace)

    result = CliRunner().invoke(cli, [str(outside), _payload()])

    assert result.exit_code != 0
    assert "approved workspace location" in str(result.exception)
    assert outside.read_bytes() == original


def test_quality_check_reports_nested_backstory_advisory(tmp_path: Path) -> None:
    path = _pursuit(tmp_path, backstory=True)

    result = CliRunner().invoke(cli, ["--quality-check", "--file", str(path), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "mode": "quality-check",
        "file": str(path),
        "has_frontmatter": True,
        "advisory_count": 1,
    }


def test_quality_check_invalid_yaml_is_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "invalid.md"
    path.write_text("---\nstage: [unterminated\n---\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["--quality-check", "--file", str(path)])

    assert result.exit_code != 0
    assert "Invalid pursuit frontmatter" in str(result.exception)
    assert "PASS" not in result.stdout


def test_validate_reports_schema_failure_as_partial(tmp_path: Path) -> None:
    path = tmp_path / "invalid.md"
    path.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["--validate", "--file", str(path), "--json"])

    assert result.exit_code == 1
    assert "INVALID" in result.stderr
    assert json.loads(result.stdout)["status"] == "invalid"


def test_validate_accepts_schema_valid_document(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)

    result = CliRunner().invoke(cli, ["--validate", "--file", str(path)])

    assert result.exit_code == 0
    assert result.stdout == f"VALID: {path}\n"


def test_template_validation_is_explicitly_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".template" / "example.md"
    path.parent.mkdir()
    path.write_text("placeholder\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["--validate", "--file", str(path), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "skipped"
    assert "SKIP" in result.stderr


@pytest.mark.parametrize("option", ["--quality-check", "--validate"])
def test_inspection_modes_require_file(option: str) -> None:
    result = CliRunner().invoke(cli, [option])

    assert result.exit_code == 2


def test_dry_run_is_rejected_for_inspection_mode(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--validate", "--file", str(tmp_path / "x.md"), "--dry-run"])

    assert result.exit_code == 2
    assert "only valid" in result.output


def test_inspection_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--validate", "--quality-check", "--file", str(tmp_path / "x.md")],
    )

    assert result.exit_code == 2
