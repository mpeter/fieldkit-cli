"""Smoke tests for the unified fieldkit CLI dispatcher.

These tests verify that the top-level dispatcher and group routing work
correctly. They do NOT test business logic — that belongs in the
per-module test files.
"""

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import cli

pytestmark = pytest.mark.unit

runner = CliRunner()

# ── Top-level dispatcher ───────────────────────────────────────────────────


def test_help_exits_0() -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "gmail" in result.output
    assert "sf" in result.output
    assert "pipeline" in result.output
    assert "brief" in result.output


def test_no_args_shows_help() -> None:
    """Click groups show help and exit 0 when invoked with no subcommand."""
    result = runner.invoke(cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_unknown_group_exits_nonzero() -> None:
    result = runner.invoke(cli, ["nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output or "No such command" in result.output


# ── gmail group ────────────────────────────────────────────────────────────


def test_gmail_help_exits_0() -> None:
    result = runner.invoke(cli, ["gmail", "--help"])
    assert result.exit_code == 0
    assert "sync" in result.output
    assert "query" in result.output


def test_gmail_sync_help_exits_0() -> None:
    result = runner.invoke(cli, ["gmail", "sync", "--help"])
    assert result.exit_code == 0
    assert "--max-messages" in result.output


def test_gmail_query_help_exits_0() -> None:
    result = runner.invoke(cli, ["gmail", "query", "--help"])
    assert result.exit_code == 0


def test_gmail_apply_intel_removed() -> None:
    # apply-intel was removed — enrich-pursuits is the replacement
    result = runner.invoke(cli, ["gmail", "apply-intel"])
    assert result.exit_code != 0


# ── sf group ──────────────────────────────────────────────────────────────


def test_sf_help_exits_0() -> None:
    result = runner.invoke(cli, ["sf", "--help"])
    assert result.exit_code == 0
    assert "listview" in result.output


def test_sf_listview_help_exits_0() -> None:
    result = runner.invoke(cli, ["sf", "listview", "--help"])
    assert result.exit_code == 0


# ── pipeline group ────────────────────────────────────────────────────────


def test_pipeline_help_exits_0() -> None:
    result = runner.invoke(cli, ["pipeline", "--help"])
    assert result.exit_code == 0


def test_pipeline_help_shows_options() -> None:
    result = runner.invoke(cli, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "--no-llm" in result.output


# ── brief group ───────────────────────────────────────────────────────────


def test_brief_help_exits_0() -> None:
    result = runner.invoke(cli, ["brief", "--help"])
    assert result.exit_code == 0


def test_brief_help_shows_options() -> None:
    result = runner.invoke(cli, ["brief", "generate", "--help"])
    assert result.exit_code == 0
    assert "--no-llm" in result.output


# ── setup group ───────────────────────────────────────────────────────────


def test_init_help_exits_0() -> None:
    result = runner.invoke(cli, ["init", "--help"])
    assert result.exit_code == 0


# ── shadowbot group ───────────────────────────────────────────────────────


def test_shadowbot_help_exits_0() -> None:
    result = runner.invoke(cli, ["shadowbot", "--help"])
    assert result.exit_code == 0
    assert "query" in result.output
    assert "auth" in result.output


def test_shadowbot_query_help_exits_0() -> None:
    result = runner.invoke(cli, ["shadowbot", "query", "--help"])
    assert result.exit_code == 0
    assert "--timeout" in result.output


# ── contact group ─────────────────────────────────────────────────────────


def test_contact_help_exits_0() -> None:
    result = runner.invoke(cli, ["contact", "--help"])
    assert result.exit_code == 0
    assert "enrich" in result.output


def test_contact_enrich_help_exits_0() -> None:
    result = runner.invoke(cli, ["contact", "enrich", "--help"])
    assert result.exit_code == 0


# ── shell completion ───────────────────────────────────────────────────────


# ── TestShellCompletion (flattened) ─────────────────────────────────────────


def test_shell_completion_bash_completion_script() -> None:
    result = runner.invoke(
        cli,
        [],
        env={"_FIELDKIT_COMPLETE": "bash_source"},
        prog_name="fieldkit",
    )
    assert result.exit_code == 0
    assert "_fieldkit_completion" in result.output


def test_shell_completion_zsh_completion_script() -> None:
    result = runner.invoke(
        cli,
        [],
        env={"_FIELDKIT_COMPLETE": "zsh_source"},
        prog_name="fieldkit",
    )
    assert result.exit_code == 0
    assert "_fieldkit_completion" in result.output
