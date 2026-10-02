"""Tests for fieldkit.pursuit.create_cmd — pursuit file scaffolding.

Task 10.2: contract tests for the `pursuit create` CLI command.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli
from fieldkit.commands.pursuit.create_cmd import cli as create_cli
from fieldkit.commands.pursuit.pipeline_health import cli as health_cli
from fieldkit.pursuit.io import write_frontmatter_raw

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_account(tmp_path: Path, account: str = "acme") -> Path:
    """Create a minimal accounts/<account>/ directory and return the data root."""
    account_dir = tmp_path / "accounts" / account
    account_dir.mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Task 10.2 tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_cmd_creates_pursuit_file(tmp_path: Path) -> None:
    """create_cmd writes a new pursuit file with compliant frontmatter."""
    root = _setup_account(tmp_path, "acme")

    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        runner = CliRunner()
        result = runner.invoke(
            create_cli,
            ["--account", "acme", "--name", "new-deal"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}:\n{result.output}"

    pursuit_file = root / "accounts" / "acme" / "pursuits" / "new-deal.md"
    assert pursuit_file.exists(), "Pursuit file must be created on disk"

    content = pursuit_file.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert frontmatter["stage"] == "pre-pipeline"
    assert frontmatter["sf_opportunity_id"] == ""
    assert frontmatter["gate-status"] == "pending"
    assert "meddpicc" not in frontmatter
    assert "legacy_meddpicc" not in frontmatter


@pytest.mark.unit
def test_create_cmd_uses_canonical_atomic_frontmatter_writer(tmp_path: Path) -> None:
    root = _setup_account(tmp_path, "acme")

    with (
        patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root),
        patch("fieldkit.commands.pursuit.create_cmd.write_frontmatter_raw", wraps=write_frontmatter_raw) as write_raw,
    ):
        result = CliRunner().invoke(create_cli, ["--account", "acme", "--name", "new-deal"])

    assert result.exit_code == 0
    assert write_raw.call_count == 1
    assert write_raw.call_args.kwargs == {"create": True, "exclusive_create": True}


@pytest.mark.unit
def test_create_cmd_refuses_file_created_after_preflight(tmp_path: Path) -> None:
    root = _setup_account(tmp_path, "acme")
    target = root / "accounts" / "acme" / "pursuits" / "new-deal.md"
    competing_content = "created by another process\n"

    def competing_create(path: str | Path, frontmatter: dict[str, object], body: str, **kwargs: object) -> None:
        target.write_text(competing_content, encoding="utf-8")
        write_frontmatter_raw(
            path,
            frontmatter,
            body,
            create=bool(kwargs["create"]),
            exclusive_create=bool(kwargs["exclusive_create"]),
        )

    with (
        patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root),
        patch("fieldkit.commands.pursuit.create_cmd.write_frontmatter_raw", side_effect=competing_create),
    ):
        result = CliRunner().invoke(create_cli, ["--account", "acme", "--name", "new-deal"])

    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == competing_content


@pytest.mark.unit
def test_create_cmd_exits_nonzero_missing_account_dir(tmp_path: Path) -> None:
    """create_cmd exits non-zero when the account directory does not exist."""
    # Create data root with a different account, not the one we request
    root = _setup_account(tmp_path, "other-account")

    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        runner = CliRunner()
        result = runner.invoke(
            create_cli,
            ["--account", "nonexistent-account", "--name", "some-deal"],
        )

    # Missing account dir → exit 3 (data error per exit code taxonomy)
    assert result.exit_code != 0, "Expected non-zero exit when account directory is missing"
    assert result.exit_code == 3, f"Expected exit 3 (data error), got {result.exit_code}"


@pytest.mark.unit
def test_create_cmd_json_dry_run_does_not_write(tmp_path: Path) -> None:
    root = _setup_account(tmp_path, "acme")
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        result = CliRunner().invoke(create_cli, ["--account", "acme", "--name", "New Deal", "--dry-run", "--json"])

    payload = json.loads(result.output)
    target = root / "accounts" / "acme" / "pursuits" / "new-deal.md"
    assert result.exit_code == 0
    assert payload == {
        "account": "acme",
        "created": False,
        "dry_run": True,
        "path": str(target),
        "slug": "new-deal",
    }
    assert not target.exists()
    assert not target.parent.exists()


@pytest.mark.unit
def test_create_cmd_text_dry_run_keeps_workspace_unchanged(tmp_path: Path) -> None:
    root = _setup_account(tmp_path, "acme")
    pursuits_dir = root / "accounts" / "acme" / "pursuits"

    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        for _ in range(2):
            result = CliRunner().invoke(create_cli, ["--account", "acme", "--name", "New Deal", "--dry-run"])
            assert result.exit_code == 0
            assert f"Would create: {pursuits_dir / 'new-deal.md'}" in result.output
            assert not pursuits_dir.exists()


@pytest.mark.unit
def test_create_cmd_default_stage_guidance_has_read_only_verification(tmp_path: Path) -> None:
    root = _setup_account(tmp_path, "acme")
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        created = CliRunner().invoke(create_cli, ["--account", "acme", "--name", "First"])

    target = root / "accounts" / "acme" / "pursuits" / "first.md"
    assert created.exit_code == 0
    assert target.exists()
    assert "pre-pipeline pursuits" in created.output
    assert "even with --include-prospect" in created.output
    assert "fieldkit pursuit audit --account acme --json" in created.output
    assert "fieldkit pursuit health --account acme --include-prospect" in created.output

    before = {path.relative_to(root) for path in root.rglob("*")}
    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=root):
        audit = CliRunner().invoke(audit_cli, ["--account", "acme", "--json"])
    assert audit.exit_code in {0, 1}  # Incomplete scaffold may produce findings.
    assert any(item["relative_path"] == "acme/pursuits/first.md" for item in json.loads(audit.stdout))
    assert {path.relative_to(root) for path in root.rglob("*")} == before

    with patch("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", return_value=root):
        health = CliRunner().invoke(health_cli, ["--account", "acme", "--include-prospect", "--json"])
    assert health.exit_code == 3  # The default pre-pipeline stage is not in health.
