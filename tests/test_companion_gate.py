"""Tests for fieldkit.companion.gate — tier checks and exact-argv matching."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fieldkit.commands.companion.cli import _act_policy_validation, cli
from fieldkit.companion.gate import (
    NO_ACT_POLICY,
    ValidatedActPolicy,
    is_allowed,
)
from fieldkit.companion.runner import ActionResult

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tier", ["read", "propose", "act"])
@pytest.mark.parametrize("invalid_allowlist", [False, True])
def test_allowed_and_run_apply_same_tier_allowlist_policy(
    monkeypatch: pytest.MonkeyPatch, tier: str, invalid_allowlist: bool
) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: tier)
    monkeypatch.setattr(
        "fieldkit.config.get_companion_act_allowlist", lambda: ["sf set-field"] if invalid_allowlist else []
    )
    calls: list[list[str]] = []

    def run_action(argv: list[str], **_kwargs: object) -> ActionResult:
        calls.append(argv)
        return ActionResult(argv=argv, exit_code=0, denied=False, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.companion.runner.run_action", run_action)
    allowed = CliRunner().invoke(cli, ["allowed", "--", "version"])
    run = CliRunner().invoke(cli, ["run", "--", "version"])

    expected = 3 if tier == "act" and invalid_allowlist else 0
    assert allowed.exit_code == expected
    assert run.exit_code == expected
    assert len(calls) == (0 if expected else 1)


@pytest.mark.parametrize(
    ("argv", "tier", "allowlist", "expected"),
    [
        # read-only table is permitted at every tier
        (["pursuit", "health"], "read", [], True),
        (["pursuit", "health", "--json"], "read", [], True),
        (["gmail", "query", "account", "acme"], "propose", [], True),
        (["version"], "read", [], True),
        # writes denied below act tier
        (["sf", "set-next-steps", "006XX", "text"], "read", [], False),
        (["sf", "set-next-steps", "006XX", "text"], "propose", [], False),
        # act tier: only exact-argv allowlist entries
        (["pursuit", "advance", "acme/deal", "--dry-run"], "act", ["pursuit advance acme/deal --dry-run"], True),
        # entry flag missing from invocation → denied (exact-argv, not prefix)
        (["pursuit", "advance", "acme/deal"], "act", ["pursuit advance --dry-run"], False),
        # different subcommand never matches a sibling entry
        (["sf", "set-field", "006XX"], "act", ["sf set-next-steps"], False),
        # act tier with empty allowlist denies writes
        (["sf", "set-next-steps", "006XX"], "act", [], False),
        # unknown tier fails closed — even for read-only commands
        (["pursuit", "health"], "admin", [], False),
        (["pursuit", "health"], "", [], False),
        # empty argv denied
        ([], "act", ["pursuit advance --dry-run"], False),
    ],
)
def test_is_allowed(argv: list[str], tier: str, allowlist: list[str], expected: bool) -> None:
    policy = NO_ACT_POLICY
    if allowlist == ["pursuit advance acme/deal --dry-run"]:
        validation = _act_policy_validation("act", allowlist)
        assert validation.policy is not None
        policy = validation.policy
    result = is_allowed(argv, tier, policy)
    assert result is expected


def test_validate_allowlist_flags_missing_dry_run_support() -> None:
    result = _act_policy_validation("act", ["pursuit advance --dry-run", "sf set-next-steps", "badentry"])
    assert len(result.problems) == 2
    assert result.policy is None
    assert any("sf set-next-steps" in p for p in result.problems)
    assert any("badentry" in p for p in result.problems)


def test_validate_allowlist_accepts_covered_entries() -> None:
    result = _act_policy_validation("act", ["pursuit advance --dry-run"])
    assert result.problems == ()
    assert isinstance(result.policy, ValidatedActPolicy)


def test_raw_allowlist_cannot_authorize_act_command() -> None:
    raw_policy: Any = ["watch run pursuit-stalls --dry-run"]
    result = is_allowed(
        ["watch", "run", "pursuit-stalls", "--dry-run"],
        "act",
        raw_policy,
    )

    assert result is False


def test_allowlist_rejects_preview_with_conflicting_write_effect() -> None:
    result = _act_policy_validation("act", ["watch run pursuit-stalls --dry-run --scrub-duplicates"])

    assert result.policy is None
    assert "conflicting effect" in result.problems[0]


@pytest.mark.parametrize(
    ("entry", "valid"),
    [
        ("watch run pursuit-stalls --dry-run", True),
        ("watch run pursuit-stalls --account ack --dry-run", True),
        ("watch run pursuit-stalls --dry-run ack acme/deal", False),
        ("watch run pursuit-stalls ack acme/deal", False),
        ("watch pursuit-stalls --dry-run", False),
        ("watch run pursuit-stalls --account acme", False),
        ("watch run pursuit-stalls --account --dry-run", False),
    ],
)
def test_allowlist_resolves_executable_groups_without_blessing_children(entry: str, valid: bool) -> None:
    result = _act_policy_validation("act", [entry])

    assert (result.problems == ()) is valid


@pytest.mark.parametrize(
    "entry",
    [
        "pursuit advance acme/deal",
        "pursuit advance acme/deal --account --dry-run",
        "pursuit advance acme/deal --account=--dry-run",
        "pursuit advance acme/deal -- --dry-run",
        "pursuit advance acme/deal --dry-run=true",
    ],
)
def test_allowlist_requires_parsed_preview_flag_for_leaf(entry: str) -> None:
    result = _act_policy_validation("act", [entry])
    assert result.problems


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (["watch", "run", "pursuit-stalls", "--dry-run"], 0),
        (["watch", "run", "pursuit-stalls", "--account", "ack", "--dry-run"], 0),
        (["watch", "run", "pursuit-stalls", "--dry-run", "ack", "acme/deal"], 3),
        (["watch", "run", "pursuit-stalls", "--account", "--dry-run"], 3),
        (["watch", "run", "pursuit-stalls", "--account", "acme"], 3),
    ],
)
def test_act_allowlist_uses_actual_nested_command_boundary(
    monkeypatch: pytest.MonkeyPatch, target: list[str], expected: int
) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [" ".join(target)])

    result = CliRunner().invoke(cli, ["allowed", "--", *target])

    assert result.exit_code == expected


def test_cli_allowed_exit_zero_for_read_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "pursuit", "health"])
    assert result.exit_code == 0
    assert "allowed" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["allowed", "version", "--json"],
        ["run", "version"],
        ["allowed", "version", "--", "version"],
        ["run", "--"],
    ],
)
def test_target_commands_require_explicit_separator(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("missing separator must not execute a target")

    monkeypatch.setattr("fieldkit.companion.runner.run_action", unexpected_run)
    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 3
    assert "separator" in result.output


def test_allowed_preserves_target_json_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])
    result = CliRunner().invoke(cli, ["allowed", "--json", "--", "version", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.output)["command"] == ["version", "--json"]
    assert json.loads(result.output)["allowed"] is False


@pytest.mark.parametrize("command", ["allowed", "run"])
def test_target_command_help_remains_available(command: str) -> None:
    result = CliRunner().invoke(cli, [command, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_denial_diagnostics_escape_invisible_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])

    result = CliRunner().invoke(cli, ["allowed", "--", "contact", "find", "Ada\u202eLovelace"])

    assert result.exit_code == 3
    assert "\u202e" not in result.output
    assert "\\u202e" in result.output


def test_cli_allowed_exit_three_on_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "sf", "set-next-steps", "006XX", "text"])
    assert result.exit_code == 3


def test_cli_allowed_act_tier_exact_argv_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    # The canonical exact-argv case: entry requires --dry-run, invocation omits it.
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["pursuit advance acme/deal --dry-run"])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "pursuit", "advance", "acme/deal"])
    assert result.exit_code == 3

    permitted = runner.invoke(cli, ["allowed", "--", "pursuit", "advance", "acme/deal", "--dry-run"])
    assert permitted.exit_code == 0


def test_cli_allowed_denies_on_malformed_act_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end reachability: validate_allowlist is actually wired into `companion allowed`,
    # not just defined (implementation note). A non-previewable allowlist entry is caught here, before
    # the per-invocation is_allowed check ever runs.
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["sf set-next-steps"])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "sf", "set-next-steps", "006XX", "text"])
    assert result.exit_code == 3
    assert "act_allowlist config error" in result.output


def test_cli_allowed_denies_on_malformed_act_allowlist_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # --json must still emit a structured document on the validate_allowlist
    # short-circuit path, not plain stderr text (the SystemExit previously
    # fired before the `if as_json:` branch was ever reached).
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["sf set-next-steps"])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--json", "--", "sf", "set-next-steps", "006XX", "text"])
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["allowed"] is False
    assert payload["tier"] == "act"
    assert payload["command"] == ["sf", "set-next-steps", "006XX", "text"]
    assert any("sf set-next-steps" in p for p in payload["error"])


def test_cli_loop_rejects_invalid_allowlist_at_act_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["sf set-field"])

    result = CliRunner().invoke(cli, ["loop", "--once"])

    assert result.exit_code == 3
    assert "act_allowlist config error" in result.output


def test_cli_run_rejects_invalid_allowlist_at_act_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["sf set-field"])

    result = CliRunner().invoke(cli, ["run", "--", "sf", "set-field"])

    assert result.exit_code == 3
    assert "act_allowlist config error" in result.output


@pytest.mark.parametrize("companion_command", ["allowed", "run"])
def test_companion_boundary_rejects_preview_with_conflicting_effect_before_writes(
    companion_command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr(
        "fieldkit.config.get_companion_act_allowlist",
        lambda: ["watch run pursuit-stalls --dry-run --scrub-duplicates"],
    )
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)

    result = CliRunner().invoke(
        cli,
        [companion_command, "--", "watch", "run", "pursuit-stalls", "--dry-run", "--scrub-duplicates"],
    )

    assert result.exit_code == 3
    assert "conflicting effect" in result.output
    assert list(tmp_path.iterdir()) == []
