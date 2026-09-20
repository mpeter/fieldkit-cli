"""Tests for fieldkit.companion.gate — tier checks and exact-argv matching."""

import json

import pytest
from click.testing import CliRunner

from fieldkit.commands.companion.cli import cli
from fieldkit.companion.gate import is_allowed, validate_allowlist

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("argv", "tier", "allowlist", "expected"),
    [
        # read-only table is permitted at every tier
        (["pursuit", "health"], "read", [], True),
        (["pursuit", "health", "--json"], "read", [], True),
        (["gmail", "query", "--account", "acme"], "propose", [], True),
        (["version"], "read", [], True),
        # writes denied below act tier
        (["sf", "set-next-steps", "006XX", "text"], "read", [], False),
        (["sf", "set-next-steps", "006XX", "text"], "propose", [], False),
        # act tier: only exact-argv allowlist entries
        (["pursuit", "advance", "acme/deal", "--dry-run"], "act", ["pursuit advance --dry-run"], True),
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
    result = is_allowed(argv, tier, allowlist)
    assert result is expected


def test_validate_allowlist_flags_missing_dry_run_support() -> None:
    result = validate_allowlist(
        ["pursuit advance --dry-run", "sf set-next-steps", "badentry"],
        dry_run_capable={"pursuit advance"},
    )
    assert len(result) == 2
    assert any("sf set-next-steps" in p for p in result)
    assert any("badentry" in p for p in result)


def test_validate_allowlist_accepts_covered_entries() -> None:
    result = validate_allowlist(["pursuit advance --dry-run"], dry_run_capable={"pursuit advance"})
    assert result == []


def test_cli_allowed_exit_zero_for_read_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "pursuit", "health"])
    assert result.exit_code == 0
    assert "allowed" in result.output


def test_cli_allowed_exit_three_on_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "read")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: [])
    runner = CliRunner()
    result = runner.invoke(cli, ["allowed", "--", "sf", "set-next-steps", "006XX", "text"])
    assert result.exit_code == 3


def test_cli_allowed_act_tier_exact_argv_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    # The canonical exact-argv case: entry requires --dry-run, invocation omits it.
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: ["pursuit advance --dry-run"])
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
