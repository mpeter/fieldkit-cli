"""Initialization must not replace unreadable or malformed operator inputs."""

import os
from pathlib import Path

import pytest
import yaml

import fieldkit.config as config
from fieldkit.__main__ import main
from fieldkit.commands.init.answers import load_answers
from fieldkit.commands.init.wizard import _load_existing_config, _wizard_write_config
from fieldkit.config._loader import MAX_CONFIG_UPDATE_BYTES, read_config_mapping_for_update

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("secret", ["fictional\x00value", "fictional\rvalue"])
def test_answers_preflights_unsupported_credentials_before_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    path = tmp_path / "answers.yaml"
    workspace = tmp_path / "workspace"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Example User",
                "email": "user@example.com",
                "data_dir": str(workspace),
                "oauth_client_id": "fictional-client",
                "oauth_client_secret": secret,
            }
        ),
        encoding="utf-8",
    )
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--answers", str(path)])

    assert result == 3
    assert not workspace.exists()
    assert not global_config.exists()


def test_configuration_update_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = b"custom:\n  token: first\n  token: second\n"
    path.write_bytes(original)

    with pytest.raises(config.ConfigError, match="invalid YAML"):
        read_config_mapping_for_update(path)

    assert path.read_bytes() == original


def test_answers_duplicate_keys_fail_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "answers.yaml"
    workspace = tmp_path / "workspace"
    original = f"name: First Name\nname: Second Name\nemail: user@example.com\ndata_dir: {workspace}\n".encode()
    path.write_bytes(original)
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--answers", str(path)])

    assert result == 3
    assert path.read_bytes() == original
    assert not workspace.exists()
    assert not global_config.exists()


@pytest.mark.parametrize("field", ["fictional-sensitive-key: value\n", "accounts: ['Private Customer / secret']\n"])
def test_answers_semantic_errors_do_not_reflect_operator_payload(tmp_path: Path, field: str) -> None:
    path = tmp_path / "answers.yaml"
    path.write_text(
        f"name: Example User\nemail: user@example.com\ndata_dir: {tmp_path / 'workspace'}\n{field}",
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigError) as caught:
        load_answers(path)

    assert "fictional-sensitive-key" not in str(caught.value)
    assert "Private Customer" not in str(caught.value)
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "fifo"])
def test_answers_cli_rejects_unsafe_file_without_reflecting_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = tmp_path / "answers.yaml"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.yaml"
        target.write_text("name: Example User\n", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)

    result = main(["init", "--answers", str(path)])

    assert result == 3
    assert str(tmp_path) not in capsys.readouterr().err


@pytest.mark.parametrize(
    "original",
    [b"\xff", b"oauth_client_secret: [fictional-sensitive-token\n", b"#" * (MAX_CONFIG_UPDATE_BYTES + 1)],
    ids=["undecodable", "malformed-sensitive", "oversize"],
)
def test_answers_reader_rejects_unsafe_input_without_payload(tmp_path: Path, original: bytes) -> None:
    path = tmp_path / "sensitive-answers.yaml"
    path.write_bytes(original)

    with pytest.raises(config.ConfigError, match="Could not read init answers") as caught:
        load_answers(path)

    assert str(tmp_path) not in str(caught.value)
    assert "fictional-sensitive-token" not in str(caught.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize("alias", [False, True])
def test_minimal_rejects_file_workspace_without_reflecting_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], alias: bool
) -> None:
    target = tmp_path / "private-file"
    target.write_text("private content\n", encoding="utf-8")
    selected = target
    if alias:
        selected = tmp_path / "file-alias"
        selected.symlink_to(target)
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--minimal", str(selected)])

    assert result == 3
    assert str(tmp_path) not in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "private content\n"
    assert not global_config.exists()


@pytest.mark.parametrize("mode", ["minimal", "answers"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_initialization_rejects_dangling_workspace_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, ancestor: bool
) -> None:
    outside = tmp_path / "outside"
    workspace = tmp_path / "selected"
    workspace.symlink_to(outside, target_is_directory=True)
    alias = workspace
    if ancestor:
        workspace = workspace / "workspace"
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)
    if mode == "minimal":
        argv = ["init", "--minimal", str(workspace)]
    else:
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            yaml.safe_dump({"name": "Example User", "email": "user@example.com", "data_dir": str(workspace)}),
            encoding="utf-8",
        )
        argv = ["init", "--answers", str(answers)]

    result = main(argv)

    assert result == 3
    assert alias.is_symlink()
    assert not outside.exists()
    assert not global_config.exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_minimal_accepts_existing_workspace_directory_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor: bool
) -> None:
    outside = tmp_path / "existing"
    outside.mkdir()
    workspace = tmp_path / "selected"
    workspace.symlink_to(outside, target_is_directory=True)
    alias = workspace
    if ancestor:
        workspace = workspace / "workspace"
    destination = outside / "workspace" if ancestor else outside
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--minimal", str(workspace)])

    assert result == 0
    assert alias.is_symlink()
    assert yaml.safe_load(global_config.read_text(encoding="utf-8"))["fieldkit_home"] == str(destination)
    assert (destination / "config" / "accounts.yaml").is_file()


@pytest.mark.parametrize("relative", ["config", "accounts", "data"])
def test_minimal_rejects_file_in_directory_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = b"operator content\n"
    target = workspace / relative
    target.write_bytes(original)
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--minimal", str(workspace)])

    assert result == 3
    assert target.read_bytes() == original
    assert sorted(path.name for path in workspace.iterdir()) == [relative]
    assert not global_config.exists()


@pytest.mark.parametrize("relative", ["config", "accounts", "data"])
def test_minimal_rejects_redirected_workspace_directories_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / relative).symlink_to(outside, target_is_directory=True)
    global_config = tmp_path / "global.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--minimal", str(workspace)])

    assert result == 3
    assert list(outside.iterdir()) == []
    assert sorted(path.name for path in workspace.iterdir()) == [relative]
    assert not global_config.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "accounts/acme-corp",
        "accounts/acme-corp/meetings",
        "accounts/acme-corp/account.md",
        "config",
        "config/identity.yaml",
    ],
)
def test_answers_rejects_redirected_workspace_destinations_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)
    before = sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*"))
    global_config = tmp_path / "global.yaml"
    answers = tmp_path / "answers.yaml"
    answers.write_text(
        yaml.safe_dump(
            {"name": "Example User", "email": "user@example.com", "data_dir": str(workspace), "accounts": ["Acme Corp"]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--answers", str(answers)])

    assert result == 3
    assert list(outside.iterdir()) == []
    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")) == before
    assert not global_config.exists()


def test_minimal_rechecks_global_configuration_before_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.init.minimal import initialize_minimal

    global_config = tmp_path / "global.yaml"
    global_config.write_text("custom: preserved\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    original_mkdir = Path.mkdir
    replacement = b"private: [broken\n"

    def replace_after_preflight(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if path == workspace / "config":
            global_config.write_bytes(replacement)
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(config, "CONFIG_PATH", global_config)
    monkeypatch.setattr(Path, "mkdir", replace_after_preflight)

    with pytest.raises(config.ConfigError, match="invalid YAML"):
        initialize_minimal(workspace)

    assert global_config.read_bytes() == replacement


def test_configuration_swap_to_symlink_fails_at_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    target = tmp_path / "private.yaml"
    path.write_text("name: Example User\n", encoding="utf-8")
    original = b"private: copied\n"
    target.write_bytes(original)
    original_open = os.open

    def swap_before_open(filename: Path, flags: int) -> int:
        path.unlink()
        path.symlink_to(target)
        return original_open(filename, flags)

    monkeypatch.setattr("fieldkit.util.text_snapshot.os.open", swap_before_open)

    with pytest.raises(config.ConfigError, match="configuration could not be read"):
        read_config_mapping_for_update(path)

    assert path.is_symlink()
    assert target.read_bytes() == original


def test_configuration_update_reader_rejects_oversize_input(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = b"#" * (MAX_CONFIG_UPDATE_BYTES + 1)
    path.write_bytes(original)

    with pytest.raises(config.ConfigError, match="configuration could not be read"):
        read_config_mapping_for_update(path)

    assert path.read_bytes() == original


@pytest.mark.parametrize("filename", ["accounts.yaml", "identity.yaml"])
@pytest.mark.parametrize("original", [b"private: [broken\n", b"- private\n", b"", b"\xff"])
def test_answers_preflights_existing_workspace_configuration_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, original: bytes
) -> None:
    workspace = tmp_path / "workspace"
    configuration = workspace / "config"
    configuration.mkdir(parents=True)
    target = configuration / filename
    target.write_bytes(original)
    global_config = tmp_path / "global.yaml"
    answers = tmp_path / "answers.yaml"
    answers.write_text(
        yaml.safe_dump(
            {"name": "Example User", "email": "user@example.com", "data_dir": str(workspace), "accounts": ["Acme Corp"]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--answers", str(answers)])

    assert result == 3
    assert target.read_bytes() == original
    assert sorted(path.name for path in configuration.iterdir()) == [filename]
    assert not (workspace / "accounts").exists()
    assert not global_config.exists()


@pytest.mark.parametrize("original", [b"private: [broken\n", b"- private\n", b"", b"\xff"])
def test_answers_initialization_preserves_invalid_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: bytes
) -> None:
    global_config = tmp_path / "config.yaml"
    global_config.write_bytes(original)
    workspace = tmp_path / "workspace"
    answers = tmp_path / "answers.yaml"
    answers.write_text(
        yaml.safe_dump({"name": "Example User", "email": "user@example.com", "data_dir": str(workspace)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = main(["init", "--answers", str(answers)])

    assert result == 3
    assert global_config.read_bytes() == original
    assert not workspace.exists()


def test_config_changed_after_defaults_read_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_config = tmp_path / "config.yaml"
    global_config.write_text("name: Example User\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", global_config)

    result = _load_existing_config()
    assert result == {"name": "Example User"}
    original = b"private: [broken\n"
    global_config.write_bytes(original)

    with pytest.raises(config.ConfigError, match="configuration"):
        _wizard_write_config(tmp_path / "workspace", "Example User", "user@example.com", "", "", "")

    assert global_config.read_bytes() == original
