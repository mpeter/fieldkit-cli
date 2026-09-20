"""Orchestration-only coverage for ``_run_wizard`` in commands/init/wizard.py.

``_run_wizard`` itself contains no business logic — it sequences five helper
functions (``_load_existing_config``, ``_wizard_prompt_inputs``,
``_wizard_confirm``, ``_wizard_write_artifacts``, ``_wizard_post_setup``) that
are each tested independently elsewhere. These tests mock all five helpers
and assert only on ``_run_wizard``'s own orchestration: which helper is
called with what arguments, in what order, and how the confirm/cancel/decline
return values map to ``_run_wizard``'s exit code and downstream calls.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.init.answers import InitInputs
from fieldkit.commands.init.wizard import _run_wizard

pytestmark = pytest.mark.unit


def _make_inputs() -> InitInputs:
    return InitInputs(
        role="Senior Manager / Client Partner",
        company="Acme Corp",
        name="Jane Doe",
        email="jane.doe@example.com",  # pii-guard: ignore
        territory="Named Accounts - West",
        salesforce_user_id="005Dn000001abcD",
        data_dir=Path("/tmp/fieldkit-data-fixture"),
        account_names=("Acme Corp",),
        gcp_project="acme-gcp-project",
        oauth_id="acme-oauth-client-id",
        oauth_secret="acme-oauth-client-secret",
        shadowbot_assistant_id="acme-shadowbot-id",
    )


# ---------------------------------------------------------------------------
# Behavior 1 — "Existing config found" display branch
# ---------------------------------------------------------------------------


def test_existing_config_message_shown_when_config_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("role: x\n", encoding="utf-8")

    with (
        patch("fieldkit.config.CONFIG_PATH", config_path),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=_make_inputs()),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=False),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts") as mock_write,
        patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post,
    ):
        result = _run_wizard()

    assert result == 0
    out = capsys.readouterr().out
    assert "Existing config found" in out
    assert str(config_path) in out
    mock_write.assert_not_called()
    mock_post.assert_not_called()


def test_existing_config_message_absent_when_no_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "missing-config.yaml"
    assert not config_path.exists()

    with (
        patch("fieldkit.config.CONFIG_PATH", config_path),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=_make_inputs()),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=False),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts"),
        patch("fieldkit.commands.init.wizard._wizard_post_setup"),
    ):
        result = _run_wizard()

    assert result == 0
    out = capsys.readouterr().out
    assert "Existing config found" not in out


# ---------------------------------------------------------------------------
# Behavior 2 — _wizard_prompt_inputs receives exactly what _load_existing_config returned
# ---------------------------------------------------------------------------


def test_prompt_inputs_called_with_load_existing_config_result(tmp_path: Path) -> None:
    sentinel_cfg = {"role": "sentinel-value"}  # unique object, checked by identity below

    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value=sentinel_cfg),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=_make_inputs()) as mock_prompt_inputs,
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=False),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts"),
        patch("fieldkit.commands.init.wizard._wizard_post_setup"),
    ):
        result = _run_wizard()

    assert result == 0
    mock_prompt_inputs.assert_called_once()
    called_arg = mock_prompt_inputs.call_args.args[0]
    assert called_arg is sentinel_cfg  # same object threaded through, not a fresh call or None


# ---------------------------------------------------------------------------
# Behavior 3 — _wizard_confirm positional arg order
# ---------------------------------------------------------------------------


def test_confirm_called_with_correct_positional_order(tmp_path: Path) -> None:
    inputs = _make_inputs()

    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=inputs),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=False) as mock_confirm,
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts"),
        patch("fieldkit.commands.init.wizard._wizard_post_setup"),
    ):
        result = _run_wizard()

    assert result == 0
    mock_confirm.assert_called_once_with(
        inputs.name,
        inputs.email,
        inputs.role,
        inputs.company,
        inputs.territory,
        inputs.salesforce_user_id,
        list(inputs.account_names),
        inputs.data_dir,
        inputs.gcp_project,
        inputs.oauth_id,
        inputs.shadowbot_assistant_id,
    )


# ---------------------------------------------------------------------------
# Behaviors 4/5 — None (cancel) vs False (decline) are distinguished
# ---------------------------------------------------------------------------


def test_confirm_none_returns_1_without_write_or_post_setup(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=_make_inputs()),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=None),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts") as mock_write,
        patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post,
    ):
        result = _run_wizard()

    assert result == 1
    mock_write.assert_not_called()
    mock_post.assert_not_called()


def test_confirm_false_returns_0_without_write_or_post_setup(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=_make_inputs()),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=False),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts") as mock_write,
        patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post,
    ):
        result = _run_wizard()

    assert result == 0
    mock_write.assert_not_called()
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior 6 — True proceeds: write_artifacts then post_setup, arg order preserved
# ---------------------------------------------------------------------------


def test_confirm_true_writes_then_post_setup_and_returns_0(tmp_path: Path) -> None:
    inputs = _make_inputs()

    call_order: list[str] = []

    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs", return_value=inputs),
        patch("fieldkit.commands.init.wizard._wizard_confirm", return_value=True),
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts") as mock_write,
        patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post,
    ):
        mock_write.side_effect = lambda *a, **kw: call_order.append("write")
        mock_post.side_effect = lambda *a, **kw: call_order.append("post_setup")

        result = _run_wizard()

    assert result == 0

    mock_write.assert_called_once_with(
        inputs.name,
        inputs.email,
        inputs.role,
        inputs.company,
        inputs.territory,
        inputs.salesforce_user_id,
        list(inputs.account_names),
        inputs.data_dir,
        inputs.gcp_project,
        inputs.oauth_id,
        inputs.oauth_secret,
        inputs.shadowbot_assistant_id,
    )
    mock_post.assert_called_once_with(
        inputs.oauth_id, inputs.gcp_project, inputs.shadowbot_assistant_id, install_skills=True
    )
    assert call_order == ["write", "post_setup"]


def test_answers_mode_does_not_start_an_interactive_skill_install(tmp_path: Path) -> None:
    """Unattended initialization must not launch a prompt-owning subprocess."""
    answers_path = tmp_path / "answers.yaml"
    answers = _make_inputs()

    with (
        patch("fieldkit.config.CONFIG_PATH", tmp_path / "missing-config.yaml"),
        patch("fieldkit.commands.init.wizard.load_answers", return_value=answers),
        patch("fieldkit.commands.init.wizard._load_existing_config", return_value={}),
        patch("fieldkit.commands.init.wizard._wizard_prompt_inputs") as mock_prompt,
        patch("fieldkit.commands.init.wizard._wizard_confirm") as mock_confirm,
        patch("fieldkit.commands.init.wizard._wizard_write_artifacts") as mock_write,
        patch("fieldkit.commands.init.wizard._wizard_post_setup") as mock_post,
    ):
        result = _run_wizard(answers_path)

    assert result == 0
    mock_prompt.assert_not_called()
    mock_confirm.assert_not_called()
    mock_write.assert_called_once()
    mock_post.assert_called_once_with(
        answers.oauth_id, answers.gcp_project, answers.shadowbot_assistant_id, install_skills=False
    )
