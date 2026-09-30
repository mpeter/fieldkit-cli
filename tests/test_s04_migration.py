"""Integration contracts for canonical Salesforce pursuit parsing and publication."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import fieldkit.commands.pursuit.audit as audit_mod
import fieldkit.commands.sf.frontmatter as frontmatter_mod
import fieldkit.commands.sf.reconcile as reconcile_mod
import fieldkit.sf.sync as sync_mod
from fieldkit.errors import FieldkitError
from fieldkit.pursuit.io import read_pursuit_text_snapshot

pytestmark = pytest.mark.integration


def _make_pursuit_with_stage(workspace: Path, extra_fm: str = "") -> Path:
    path = workspace / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nstage: discover\ngate-status: pending\n{extra_fm}---\n"
        "\n## Key Fields\n\n| Field | Value |\n| ----- | ----- |\n| Stage | Discover |\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("identity", ["006000000000AAA", "006000000000AAAabc"])
def test_identity_reads_one_bounded_snapshot(tmp_path: Path, identity: str) -> None:
    path = _make_pursuit_with_stage(tmp_path, f"sf_opportunity_id: {identity}\n")
    with patch("fieldkit.sf.sync.read_pursuit_text_snapshot", wraps=read_pursuit_text_snapshot) as read:
        result = sync_mod.read_local_pursuit(path, workspace=tmp_path)
    assert result.opportunity_id == identity
    read.assert_called_once_with(path)


def test_missing_identity_is_untracked_not_an_error(tmp_path: Path) -> None:
    path = _make_pursuit_with_stage(tmp_path)
    assert sync_mod.read_local_pursuit(path, workspace=tmp_path).opportunity_id is None


def test_quoted_identity_real_file_roundtrip(tmp_path: Path) -> None:
    identity = "006000000000AAA"
    path = _make_pursuit_with_stage(tmp_path, f'sf_opportunity_id: "{identity}"\n')
    assert sync_mod.read_local_pursuit(path, workspace=tmp_path).opportunity_id == identity


def test_placeholder_is_not_complete_untracked_evidence(tmp_path: Path) -> None:
    path = _make_pursuit_with_stage(tmp_path, "sf_opportunity_id: NEEDS-LOOKUP\n")
    with pytest.raises(FieldkitError, match="identity is invalid"):
        sync_mod.read_local_pursuit(path, workspace=tmp_path)


def test_reconcile_preview_reads_canonical_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make_pursuit_with_stage(tmp_path, "sf_stage: Propose\n")
    monkeypatch.setattr(reconcile_mod, "get_fieldkit_home", lambda: tmp_path)
    with patch("fieldkit.sf.frontmatter.read_pursuit_text_snapshot", wraps=read_pursuit_text_snapshot) as read:
        result = CliRunner().invoke(reconcile_mod.cli, [str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    read.assert_called_once_with(path)
    assert "| Stage | Discover |" in path.read_text(encoding="utf-8")


def test_reconcile_real_file_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make_pursuit_with_stage(tmp_path, "sf_stage: Propose\n")
    monkeypatch.setattr(reconcile_mod, "get_fieldkit_home", lambda: tmp_path)
    result = CliRunner().invoke(reconcile_mod.cli, [str(path)])
    assert result.exit_code == 0, result.output
    assert "| Stage | Propose |" in path.read_text(encoding="utf-8")


# ── TestQualityCheckBackstoryScanViaLibIo (flattened) ───────────────────────


def test_quality_check_backstory_scan_via_lib_io_quality_check_real_file_with_backstory(tmp_path, capsys):
    """End-to-end: the quality check warns on nested frontmatter evidence."""
    content = (
        '---\nstage: discover\nsf_next_steps: "[Backstory] schedule follow-up via People.AI signal"\n---\n# Test\n'
    )
    p = tmp_path / "pursuit.md"
    p.write_text(content, encoding="utf-8")

    frontmatter_mod._quality_check_pursuit(str(p))
    err = capsys.readouterr().err
    assert "Backstory-derived data found" in err


def test_check_backstory_returns_frontmatter_finding_with_severity_and_message() -> None:
    findings = audit_mod._check_backstory({"champion_name": "[Backstory] Jane Smith"}, "")

    assert len(findings) == 1
    assert findings[0].level == "ERROR"
    assert "Field `champion_name` contains Backstory-derived data" in findings[0].message


@pytest.mark.parametrize(
    ("body", "level", "message"),
    [
        (
            "Notes (via Backstory) were imported.",
            "WARNING",
            "Body contains '(via Backstory)' attribution — remove or rephrase",
        ),
        (
            "## Backstory Activity\n\nImported notes.",
            "WARNING",
            "Body contains 'Backstory Activity' section header — remove",
        ),
        (
            "Engagement Score: 87",
            "WARNING",
            "Body contains 'Engagement Score' metric — remove",
        ),
    ],
)
def test_check_backstory_returns_body_finding_with_severity_and_message(body: str, level: str, message: str) -> None:
    findings = audit_mod._check_backstory({}, body)

    assert len(findings) == 1
    assert findings[0].level == level
    assert findings[0].message == message
