"""Initialization previews validate their inputs without creating runtime artifacts."""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import fieldkit.config as config
from fieldkit.__main__ import main
from fieldkit.commands.init.cli import cli

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["minimal", "answers"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_initialization_machine_result_is_payload_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, dry_run: bool
) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    if mode == "answers":
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            yaml.safe_dump({"name": "Example User", "email": "user@example.com", "data_dir": str(workspace)}),
            encoding="utf-8",
        )
        argv = ["--answers", str(answers)]
    else:
        argv = ["--minimal", str(workspace)]
    argv += ["--json", *(["--dry-run"] if dry_run else [])]

    result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "status": "preview" if dry_run else "initialized",
        "mode": mode,
        "dry_run": dry_run,
    }
    assert result.stderr == ""
    assert "Example User" not in result.output
    assert "user@example.com" not in result.output
    assert str(tmp_path) not in result.output
    assert workspace.exists() is (not dry_run)
    assert config_path.exists() is (not dry_run)


@pytest.mark.parametrize("flag", ["--json", "--dry-run"])
def test_machine_or_preview_mode_requires_noninteractive_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    result = main(["init", flag])

    assert result == 3
    assert not config_path.exists()


def test_preview_rejects_redirected_destination_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "config").symlink_to(outside, target_is_directory=True)
    config_path = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    result = main(["init", "--minimal", str(workspace), "--dry-run", "--json"])

    assert result == 3
    assert not config_path.exists()
    assert list(outside.iterdir()) == []


def test_preview_preserves_existing_workspace_and_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    accounts = workspace / "config" / "accounts.yaml"
    accounts.write_text("internal_domains: []\naccounts: {}\n", encoding="utf-8")
    config_path = tmp_path / "global.yaml"
    config_path.write_text("custom: keep\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (config_path, accounts)}

    result = CliRunner().invoke(cli, ["--minimal", str(workspace), "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "preview"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before
    assert sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")) == [
        "config",
        "config/accounts.yaml",
    ]


@pytest.mark.parametrize("mode", ["minimal", "answers"])
def test_preview_rejects_invalid_global_configuration_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "global.yaml"
    original = b"custom: first\ncustom: second\n"
    config_path.write_bytes(original)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    if mode == "answers":
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            yaml.safe_dump({"name": "Example User", "email": "user@example.com", "data_dir": str(workspace)}),
            encoding="utf-8",
        )
        argv = ["--answers", str(answers)]
    else:
        argv = ["--minimal", str(workspace)]

    result = main(["init", *argv, "--dry-run", "--json"])

    assert result == 3
    assert config_path.read_bytes() == original
    assert not workspace.exists()
