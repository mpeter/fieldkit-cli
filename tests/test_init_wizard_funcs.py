"""Unit tests for fieldkit/setup/wizard.py.

Covers the ten helper functions targeted by tasks 3.1-3.10 of the
crap-reduction OpenSpec change.  All tests are isolated: no real git,
no real filesystem outside tmp_path, no real subprocess calls.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

import fieldkit.commands.init.wizard as wizard
import fieldkit.config as fieldkit_config
from fieldkit.commands.init.answers import InitInputs, account_key, load_answers
from fieldkit.commands.init.cli import cli as setup_cli
from fieldkit.commands.init.scaffold import write_judgment_configs
from fieldkit.commands.init.wizard import (
    _compute_fieldkit_root,
    _load_existing_identity,
    _prompt,
    _wizard_confirm,
    _wizard_post_setup,
    _wizard_write_config,
    _write_accounts_yaml,
)
from fieldkit.config import ConfigError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. _write_accounts_yaml — creates file and contains both account names
# ---------------------------------------------------------------------------


def test_write_accounts_yaml_creates_file(tmp_path: Path) -> None:
    """_write_accounts_yaml writes accounts.yaml with one entry per name."""
    account_names = ["Acme Corp", "Globalpay"]
    _write_accounts_yaml(tmp_path, account_names)

    config_path = tmp_path / "config" / "accounts.yaml"
    assert config_path.exists(), "accounts.yaml must be created"

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    accounts = data.get("accounts", {})
    # Keys are lowercased, spaces replaced with hyphens
    assert "acme-corp" in accounts, "Acme Corp must appear as 'acme-corp'"
    assert "globalpay" in accounts, "Globalpay must appear as 'globalpay'"


# ---------------------------------------------------------------------------
# 2. _wizard_write_config — merges existing keys (custom key preserved)
# ---------------------------------------------------------------------------


def test_wizard_write_config_merges_existing_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running _wizard_write_config preserves keys it does not manage."""
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    # Pre-seed config with a custom key that setup does not write.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"my_custom_key": "keep-me"}), encoding="utf-8")

    # Patch CONFIG_PATH and cfg.__file__ so the function writes to our tmp path.
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(fieldkit_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py"))

    # Also patch _compute_fieldkit_root so no git subprocess is needed.
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: fake_root)

    _wizard_write_config(
        data_dir=data_dir,
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        gcp_project="",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result.get("my_custom_key") == "keep-me", "Custom key must be preserved after merge"
    assert result.get("name") == "Test User", "Managed key must be written"


# ---------------------------------------------------------------------------
# 3. _wizard_write_config — creates config when absent
# ---------------------------------------------------------------------------


def test_wizard_write_config_creates_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_wizard_write_config creates config.yaml from scratch when none exists."""
    config_path = tmp_path / "subdir" / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    assert not config_path.exists()

    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(fieldkit_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py"))

    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: fake_root)

    _wizard_write_config(
        data_dir=data_dir,
        name="New User",
        email="new@example.com",  # pii-guard: ignore
        role="",
        company="",
        gcp_project="",
    )

    assert config_path.exists(), "config.yaml must be created when absent"
    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result.get("fieldkit_home") == str(data_dir)


def test_wizard_write_config_replaces_malformed_existing_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed prior config cannot prevent the wizard from writing a usable replacement."""
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"
    config_path.write_text("invalid: [yaml\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: tmp_path / "fieldkit-cli")

    _wizard_write_config(
        data_dir=data_dir,
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        gcp_project="",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["fieldkit_home"] == str(data_dir)
    assert result["name"] == "Test User"


# ---------------------------------------------------------------------------
# 4. _load_existing_identity — returns {} when path is missing
# ---------------------------------------------------------------------------


def test_load_existing_identity_returns_empty_when_missing(tmp_path: Path) -> None:
    """_load_existing_identity returns {} for a nonexistent data_dir."""
    nonexistent = tmp_path / "no-such-dir"
    result = _load_existing_identity(nonexistent)
    assert result == {}, "Must return empty dict when identity.yaml does not exist"


# ---------------------------------------------------------------------------
# 5. _load_existing_identity — returns parsed dict for valid YAML
# ---------------------------------------------------------------------------


def test_load_existing_identity_returns_parsed_dict(tmp_path: Path) -> None:
    """_load_existing_identity parses identity.yaml and returns the inner dict."""
    identity_data = {
        "identity": {
            "name": "Jane Doe",
            "email": "jane@example.com",  # pii-guard: ignore
            "accounts": ["Acme Corp"],
        }
    }
    identity_path = tmp_path / "config" / "identity.yaml"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(yaml.dump(identity_data), encoding="utf-8")

    result = _load_existing_identity(tmp_path)

    assert result.get("name") == "Jane Doe"
    assert result.get("email") == "jane@example.com"  # pii-guard: ignore
    assert result.get("accounts") == ["Acme Corp"]


# ---------------------------------------------------------------------------
# 6. _compute_fieldkit_root — mocked subprocess, finds pyproject.toml
# ---------------------------------------------------------------------------


def test_compute_fieldkit_root_finds_pyproject_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_compute_fieldkit_root returns the git root when pyproject.toml is present there."""
    # Create a fake repo root with pyproject.toml so the candidate check passes.
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    (fake_root / "pyproject.toml").write_text("[project]\nname = 'fake'\n", encoding="utf-8")

    # Mock subprocess.run to return our fake_root as the git toplevel.
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = str(fake_root) + "\n"

    with patch("fieldkit.commands.init.wizard.subprocess.run", return_value=mock_result) as mock_run:
        result = _compute_fieldkit_root()

    mock_run.assert_called_once()
    assert result == fake_root.resolve(), f"Expected {fake_root.resolve()}, got {result}"


# ---------------------------------------------------------------------------
# 7. _prompt — returns default when user enters empty string
# ---------------------------------------------------------------------------


def test_prompt_returns_default_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """_prompt returns the default value when the user presses Enter (empty input)."""
    # Simulate user pressing Enter (empty string).
    monkeypatch.setattr("builtins.input", lambda _: "")

    result = _prompt("Label", default="my-default", required=False)
    assert result == "my-default", "Must return default when input is empty"


# ---------------------------------------------------------------------------
# 8. _wizard_confirm — returns True when user confirms
# ---------------------------------------------------------------------------


def test_wizard_confirm_returns_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_wizard_confirm returns True when user enters 'y' (or just Enter)."""
    monkeypatch.setattr("builtins.input", lambda _: "y")

    result = _wizard_confirm(
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        territory="West",
        salesforce_user_id="005abc",
        account_names=["Acme Corp"],
        data_dir=tmp_path / "data",
        gcp_project="my-project",
        oauth_id="",
    )
    assert result is True


# ---------------------------------------------------------------------------
# 9. _wizard_confirm — returns False when user cancels
# ---------------------------------------------------------------------------


def test_wizard_confirm_returns_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_wizard_confirm returns False when user enters 'n'."""
    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = _wizard_confirm(
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        territory="",
        salesforce_user_id="",
        account_names=[],
        data_dir=tmp_path / "data",
        gcp_project="",
        oauth_id="",
    )
    assert result is False


# ---------------------------------------------------------------------------
# 10. _wizard_post_setup — emits non-empty output to stdout
# ---------------------------------------------------------------------------


def test_wizard_post_setup_emits_summary(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """_wizard_post_setup prints a non-empty summary to stdout."""
    # Prevent the real 'fieldkit skill install' subprocess from running.
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("fieldkit.commands.init.wizard.subprocess.run", return_value=mock_result):
        _wizard_post_setup(oauth_id="", gcp_project="my-gcp-project")

    captured = capsys.readouterr()
    assert captured.out.strip(), "stdout must be non-empty after _wizard_post_setup"
    assert "Add github_repo" in captured.out
    assert "Setup complete" in captured.out, "Must print setup-complete message"


def test_wizard_post_setup_only_advertises_issue_board_when_repo_is_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("github_repo: example/fieldkit-project\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    mock_result = MagicMock(returncode=0, stdout="", stderr="")

    with patch("fieldkit.commands.init.wizard.subprocess.run", return_value=mock_result):
        _wizard_post_setup(oauth_id="", gcp_project="")

    assert "Run 'fieldkit issue board'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 10b. _wizard_post_setup — shadowbot auth hint appears when assistant_id is set
# ---------------------------------------------------------------------------


def test_wizard_post_setup_emits_shadowbot_hint_when_id_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_wizard_post_setup includes the shadowbot auth hint when assistant_id is non-empty."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("fieldkit.commands.init.wizard.subprocess.run", return_value=mock_result):
        _wizard_post_setup(oauth_id="", gcp_project="", shadowbot_assistant_id="asst-xyz")

    captured = capsys.readouterr()
    assert "auth shadowbot" in captured.out, "shadowbot auth hint must appear when assistant_id is set"


# ---------------------------------------------------------------------------
# 11. _wizard_write_config — writes shadowbot block when assistant_id provided
# ---------------------------------------------------------------------------


def test_wizard_write_config_writes_shadowbot_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_wizard_write_config records an assistant ID without inventing service endpoints."""
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(fieldkit_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py"))

    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: fake_root)

    _wizard_write_config(
        data_dir=data_dir,
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        gcp_project="",
        shadowbot_assistant_id="asst-abc123",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "shadowbot" in result, "shadowbot key must be written when assistant_id is provided"
    shadowbot = result["shadowbot"]
    assert shadowbot["assistant_id"] == "asst-abc123"
    assert set(shadowbot) == {"assistant_id"}


# ---------------------------------------------------------------------------
# 12. _wizard_write_config — does NOT write shadowbot block when assistant_id empty
# ---------------------------------------------------------------------------


def test_wizard_write_config_no_shadowbot_block_when_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_wizard_write_config does not write a shadowbot: key when assistant_id is empty."""
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(fieldkit_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py"))

    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: fake_root)

    _wizard_write_config(
        data_dir=data_dir,
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        gcp_project="",
        shadowbot_assistant_id="",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "shadowbot" not in result, "shadowbot key must NOT be written when assistant_id is empty"


# ---------------------------------------------------------------------------
# 13. _wizard_write_config — re-run merges into existing shadowbot block (preserves custom keys)
# ---------------------------------------------------------------------------


def test_wizard_write_config_merges_existing_shadowbot_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running _wizard_write_config with a new assistant_id preserves user-added shadowbot keys."""
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    # Pre-seed config with an existing shadowbot block containing a custom key
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.dump(
            {
                "shadowbot": {
                    "assistant_id": "old-asst-id",
                    "custom_key": "preserve-me",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(fieldkit_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py"))

    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: fake_root)

    _wizard_write_config(
        data_dir=data_dir,
        name="Test User",
        email="test@example.com",  # pii-guard: ignore
        role="AE",
        company="Acme",
        gcp_project="",
        shadowbot_assistant_id="new-asst-id",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    shadowbot = result.get("shadowbot", {})
    assert shadowbot.get("assistant_id") == "new-asst-id", "wizard value must win for assistant_id on re-run"
    assert shadowbot.get("custom_key") == "preserve-me", "user-added keys must be preserved on re-run"
    assert "api_base" not in shadowbot


# ---------------------------------------------------------------------------
# 14. _wizard_prompt_inputs — invalid assistant_id triggers re-prompt (SEC-2)
# ---------------------------------------------------------------------------


def test_wizard_prompt_inputs_rejects_invalid_assistant_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid assistant_id causes the wizard to re-prompt until valid input is given."""
    # Sequence: bad ID → valid ID → remaining fields all use defaults
    inputs = iter(
        [
            "Senior Manager / Client Partner",  # role
            "Example Consulting",  # company
            "Test User",  # name
            "test@example.com",  # email  # pii-guard: ignore
            "",  # territory (optional)
            "",  # salesforce_user_id (optional)
            "",  # account_names (empty list ok)
            "~/fieldkit-workspace",  # data_dir
            "",  # gcp_project (optional)
            "",  # oauth_id (optional)
            "bad id with spaces",  # shadowbot assistant_id — INVALID
            "valid-asst-id",  # shadowbot assistant_id — valid on retry
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    from fieldkit.commands.init.wizard import _wizard_prompt_inputs

    result = _wizard_prompt_inputs({})
    assert result.shadowbot_assistant_id == "valid-asst-id", "Must accept the valid ID after re-prompt"

    captured = capsys.readouterr()
    assert "Invalid assistant ID" in captured.err, "Must print validation error to stderr on bad input"


# ---------------------------------------------------------------------------
# 15. _wizard_post_setup — shadowbot hint is absent when no assistant_id (TEST-5)
# ---------------------------------------------------------------------------


def test_wizard_post_setup_no_shadowbot_hint_when_id_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_wizard_post_setup does not emit shadowbot auth hint when assistant_id is empty."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("fieldkit.commands.init.wizard.subprocess.run", return_value=mock_result):
        _wizard_post_setup(oauth_id="", gcp_project="")

    captured = capsys.readouterr()
    assert "auth shadowbot" not in captured.out, "shadowbot auth hint must NOT appear when assistant_id is empty"


# ---------------------------------------------------------------------------
# 16. cli — bare invocation (no subcommand) runs the wizard and exits its code
# ---------------------------------------------------------------------------


def test_setup_cli_bare_invocation_runs_wizard() -> None:
    """`fieldkit init` with no subcommand runs the wizard and exits with its return code."""
    with patch("fieldkit.commands.init.cli._run_wizard", return_value=0) as mock_wizard:
        result = CliRunner().invoke(setup_cli, [], catch_exceptions=False)

    mock_wizard.assert_called_once_with(None)
    assert result.exit_code == 0


def test_setup_cli_bare_invocation_propagates_wizard_failure() -> None:
    """`fieldkit init` with no subcommand exits nonzero when the wizard fails."""
    with patch("fieldkit.commands.init.cli._run_wizard", return_value=1):
        result = CliRunner().invoke(setup_cli, [], catch_exceptions=False)

    assert result.exit_code == 1


def test_write_judgment_configs_creates_exact_empty_schemas(tmp_path: Path) -> None:
    result = write_judgment_configs(tmp_path)

    assert result is None
    results = {
        path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted((tmp_path / "config").glob("*.json"))
    }
    assert results == {
        "clocks.json": {"clocks": []},
        "engines.json": {"engines": []},
        "people.json": {"people": []},
        "watchlist.json": {"opportunities": []},
    }


def test_write_judgment_configs_preserves_existing_bytes_and_fills_missing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    people_path = config_dir / "people.json"
    original = b'{"people":[{"name":"Example Person"}]}\n'
    people_path.write_bytes(original)

    result = write_judgment_configs(tmp_path)

    assert result is None
    assert people_path.read_bytes() == original
    assert (config_dir / "clocks.json").exists()
    assert (config_dir / "engines.json").exists()
    assert (config_dir / "watchlist.json").exists()


def test_load_answers_returns_typed_values_and_defaults(tmp_path: Path) -> None:
    answers_path = tmp_path / "answers.yaml"
    data_dir = tmp_path / "workspace"
    answers_path.write_text(
        yaml.safe_dump(
            {
                "name": "Example User",
                "email": "user@example.com",  # pii-guard: ignore
                "data_dir": str(data_dir),
                "accounts": ["Acme Corp"],
                "oauth_client_id": "client-id",
                "oauth_client_secret": "client-secret",
                "shadowbot_assistant_id": "assistant.one",
            }
        ),
        encoding="utf-8",
    )

    result = load_answers(answers_path)

    assert result == InitInputs(
        role="Account Engineer",
        company="",
        name="Example User",
        email="user@example.com",  # pii-guard: ignore
        territory="",
        salesforce_user_id="",
        data_dir=data_dir.resolve(),
        account_names=("Acme Corp",),
        gcp_project="",
        oauth_id="client-id",
        oauth_secret="client-secret",
        shadowbot_assistant_id="assistant.one",
    )


def test_account_key_normalizes_safe_name_and_rejects_traversal() -> None:
    result = account_key("Acme Corp")

    assert result == "acme-corp"
    with pytest.raises(ConfigError, match="safe slug"):
        account_key("../../outside")


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("- not-a-mapping\n", "mapping"),
        ("name: Example User\nemail: user@example.com\n", "data_dir"),  # pii-guard: ignore
        (
            "name: Example User\nemail: user@example.com\ndata_dir: /tmp/example\nnaem: typo\n",  # pii-guard: ignore
            "Unknown",
        ),
        (
            "name: Example User\nemail: user@example.com\ndata_dir: /tmp/example\naccounts: acme\n",  # pii-guard: ignore
            "accounts",
        ),
        (
            "name: Example User\nemail: user@example.com\ndata_dir: /tmp/example\naccounts: ['../../outside']\n",  # pii-guard: ignore
            "safe slug",
        ),
        (
            "name: Example User\nemail: user@example.com\ndata_dir: /tmp/example\noauth_client_secret: secret\n",  # pii-guard: ignore
            "oauth_client_id",
        ),
        (
            "name: Example User\nemail: user@example.com\ndata_dir: /tmp/example\nshadowbot_assistant_id: bad/id\n",  # pii-guard: ignore
            "ShadowBot",
        ),
    ],
)
def test_load_answers_rejects_invalid_document(tmp_path: Path, document: str, message: str) -> None:
    answers_path = tmp_path / "answers.yaml"
    answers_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_answers(answers_path)


def test_answers_cli_passes_path_without_reading_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers_path = tmp_path / "answers.yaml"
    answers_path.write_text("name: Example\nemail: user@example.com\ndata_dir: /tmp/example\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("answers mode read stdin"))

    with patch("fieldkit.commands.init.cli._run_wizard", return_value=0) as mock_wizard:
        result = CliRunner().invoke(setup_cli, ["--answers", str(answers_path)], catch_exceptions=False)

    assert result.exit_code == 0
    mock_wizard.assert_called_once_with(answers_path.resolve())


def test_answers_option_is_rejected_with_init_subcommand(tmp_path: Path) -> None:
    answers_path = tmp_path / "answers.yaml"
    answers_path.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(setup_cli, ["--answers", str(answers_path), "migrate"])

    assert result.exit_code != 0
    assert "cannot be used with an init subcommand" in result.output


def test_answers_cli_initializes_complete_workspace_without_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers_path = tmp_path / "answers.yaml"
    data_dir = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    answers_path.write_text(
        yaml.safe_dump(
            {
                "name": "Example User",
                "email": "user@example.com",  # pii-guard: ignore
                "data_dir": str(data_dir),
                "accounts": ["Acme Corp"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("answers mode read stdin"))
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "_compute_fieldkit_root", lambda: tmp_path / "repo")

    with patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post_setup:
        result = CliRunner().invoke(setup_cli, ["--answers", str(answers_path)], catch_exceptions=False)

    assert result.exit_code == 0
    assert (
        yaml.safe_load((data_dir / "config" / "identity.yaml").read_text(encoding="utf-8"))["identity"]["name"]
        == "Example User"
    )
    assert json.loads((data_dir / "config" / "clocks.json").read_text(encoding="utf-8")) == {"clocks": []}
    assert json.loads((data_dir / "config" / "engines.json").read_text(encoding="utf-8")) == {"engines": []}
    assert json.loads((data_dir / "config" / "people.json").read_text(encoding="utf-8")) == {"people": []}
    assert json.loads((data_dir / "config" / "watchlist.json").read_text(encoding="utf-8")) == {"opportunities": []}
    mock_post_setup.assert_called_once_with("", "", "", install_skills=False)


def test_invalid_answers_exit_three_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.__main__ import main

    answers_path = tmp_path / "answers.yaml"
    data_dir = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    answers_path.write_text(
        yaml.safe_dump(
            {
                "name": "Example User",
                "email": "user@example.com",  # pii-guard: ignore
                "data_dir": str(data_dir),
                "misspelled_key": "ignored data",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)

    result = main(["init", "--answers", str(answers_path)])

    assert result == 3
    assert not data_dir.exists()
    assert not config_path.exists()


def test_minimal_cli_creates_only_generic_offline_scaffolding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("minimal init read stdin"))

    result = CliRunner().invoke(setup_cli, ["--minimal", str(workspace)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "First success: fieldkit skill list" in result.output
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config == {
        "fieldkit_home": str(workspace),
        "pipeline_db": str(workspace / "data" / "pipeline.db"),
        "gmail_db": str(workspace / "data" / "gmail.db"),
    }
    assert yaml.safe_load((workspace / "config" / "accounts.yaml").read_text(encoding="utf-8")) == {
        "internal_domains": [],
        "accounts": {},
    }
    assert not (workspace / "config" / "identity.yaml").exists()
    assert not (workspace / ".env").exists()


def test_minimal_cli_preserves_unmanaged_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("custom_setting: keep\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)

    result = CliRunner().invoke(setup_cli, ["--minimal", str(workspace)], catch_exceptions=False)

    assert result.exit_code == 0
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["custom_setting"] == "keep"


def test_minimal_cli_rejects_invalid_existing_config_before_workspace_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.__main__ import main

    workspace = tmp_path / "workspace"
    config_path = tmp_path / "global" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("- not-a-mapping\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit_config, "CONFIG_PATH", config_path)

    result = main(["init", "--minimal", str(workspace)])

    assert result == 3
    assert not workspace.exists()


def test_minimal_and_answers_are_mutually_exclusive(tmp_path: Path) -> None:
    answers_path = tmp_path / "answers.yaml"
    answers_path.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(
        setup_cli,
        ["--answers", str(answers_path), "--minimal", str(tmp_path / "workspace")],
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
