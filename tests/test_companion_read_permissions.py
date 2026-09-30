"""The companion read tier accepts only reviewed, fully parsed invocations."""

from __future__ import annotations

import click
import pytest

from fieldkit.cli_registry import walk_cli
from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed
from fieldkit.companion.mapping import READ_ONLY_POLICIES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "argv",
    [
        ["brief", "open", "--no-open", "--json"],
        ["companion", "feed", "--all", "--account", "acme-corp"],
        ["companion", "allowed", "--", "pursuit", "health", "--json"],
        ["contact", "find", "Ada Lovelace", "--json"],
        ["contact", "list", "--account", "acme-corp", "--limit", "25"],
        ["doctor", "gmail", "--json"],
        ["gmail", "query", "person", "ada@example.com", "--since", "2026-01-01", "--limit", "10"],
        ["gmail", "query", "context", "Ada Lovelace", "--excerpt", "500"],
        ["gmail", "query", "account", "acme-corp"],
        ["gmail", "query", "dig", "acme-corp", "renewal"],
        ["gmail", "query", "threads", "renewal", "--account", "acme-corp"],
        ["gmail", "query", "champion", "Ada Lovelace"],
        ["gmail", "query", "blindspots", "acme-corp", "--min-messages", "3"],
        ["gmail", "decay", "--account", "acme-corp", "--days", "90", "--limit", "50"],
        ["gmail", "backstory-gap", "--account", "acme-corp", "--min-messages", "5", "--limit", "500"],
        ["ingest", "status", "--account", "acme-corp", "--json"],
        ["ingest", "backfill", "--dry-run", "--account", "acme-corp"],
        ["issue", "list", "--status", "open", "--type", "bug"],
        ["issue", "show", "fieldkit-123", "--json"],
        ["issue", "show", "fieldkit-042"],
        ["issue", "board", "--json"],
        ["meeting", "list", "--account", "acme-corp"],
        ["pipeline", "quota", "--source", "pursuits", "--account", "acme-corp"],
        ["pipeline", "open", "--no-open", "--account", "acme-corp"],
        ["pursuit", "health", "--account", "acme-corp", "--compact"],
        ["pursuit", "health", "-a", "acme-corp"],
        ["pursuit", "health", "--account=acme-corp"],
        ["watch", "logs", "backstory-health", "-n", "5"],
        ["pursuit", "forecast", "--quota", "1000000.50"],
        ["pursuit", "projects", "--strict", "--json"],
        ["sf", "session-check", "--json"],
        ["skill", "list", "--group", "sales", "--verbose"],
        ["skill", "show", "brief", "--json"],
        ["skill", "variables"],
        ["version"],
        ["watch", "logs", "backstory-health", "--tail", "100", "--json"],
    ],
)
def test_reviewed_read_invocations_are_allowed(argv: list[str]) -> None:
    assert is_allowed(argv, "read", NO_ACT_POLICY) is True


@pytest.mark.parametrize("control", ["\x7f", "\x9b", "\u202e", "\u200b", "\u2028", "\u2029"])
@pytest.mark.parametrize("tier", ["read", "propose", "act"])
def test_invisible_control_values_are_denied(control: str, tier: str) -> None:
    argv = ["contact", "find", f"Ada{control}Lovelace"]
    assert is_allowed(argv, tier, NO_ACT_POLICY) is False


@pytest.mark.parametrize("identifier", ["field\u212ait-42", "fieldk\u0131t-42"])
def test_issue_identifiers_use_ascii_only(identifier: str) -> None:
    assert is_allowed(["issue", "show", identifier], "read", NO_ACT_POLICY) is False


@pytest.mark.parametrize(
    "argv",
    [
        ["pursuit", "health", "extra"],
        ["pursuit", "health", "--future-option"],
        ["pursuit", "health", "--account"],
        ["pursuit", "health", "--account", "--json"],
        ["pursuit", "health", "--account=--json"],
        ["pursuit", "health", "-a=acme"],
        ["gmail", "decay", "-a=acme"],
        ["gmail", "query", "threads", "renewal", "-aacme"],
        ["watch", "logs", "backstory-health", "-n=5"],
        ["watch", "logs", "backstory-health", "-n5"],
        ["pursuit", "forecast", "-q=5"],
        ["pursuit", "health", "--json=true"],
        ["contact", "find"],
        ["contact", "find", "a", "extra"],
        ["contact", "find", "a", "--db", "/tmp/private.db"],
        ["doctor", "gmail", "--db", "/tmp/private.db"],
        ["gmail", "query"],
        ["gmail", "query", "--account", "acme-corp"],
        ["gmail", "query", "future", "term"],
        ["gmail", "query", "person"],
        ["gmail", "query", "person", "Ada", "--db", "/tmp/private.db"],
        ["gmail", "query", "person", "Ada", "--since", "yesterday"],
        ["gmail", "query", "person", "Ada", "--limit", "0"],
        ["gmail", "query", "blindspots", "acme", "--known=--include-suspected"],
        ["gmail", "query", "context", "Ada", "--excerpt", "10001"],
        ["gmail", "decay"],
        ["gmail", "decay", "acme"],
        ["gmail", "decay", "acme", "--account", "other"],
        ["gmail", "decay", "--account", "acme", "--limit", "-1"],
        ["gmail", "backstory-gap", "--db=/tmp/private.db"],
        ["gmail", "backstory-gap", "--limit", "501"],
        ["ingest", "backfill"],
        ["ingest", "backfill", "--json"],
        ["issue", "list", "--status", "deleted"],
        ["issue", "list", "--module=--all"],
        ["issue", "show", "123"],
        ["issue", "show", "fieldkit-000"],
        ["pipeline", "quota", "--set", "10"],
        ["pipeline", "quota", "--period", "2026-H2"],
        ["pipeline", "quota", "--data-root", "/tmp/other"],
        ["pipeline", "quota", "--source", "future"],
        ["pipeline", "open", "--json"],
        ["version", "--json"],
        ["version", "--features"],
        ["version", "--unknown"],
        ["watch", "logs", "unknown-watcher"],
        ["watch", "logs", "backstory-health", "--tail", "0"],
        ["watch", "logs", "backstory-health", "--tail", "10001"],
        ["watch", "logs", "backstory-health", "--tail", "not-a-number"],
    ],
)
def test_malformed_ambiguous_or_unreviewed_read_invocations_are_denied(argv: list[str]) -> None:
    assert is_allowed(argv, "read", NO_ACT_POLICY) is False


@pytest.mark.parametrize(
    "argv",
    [
        ["companion", "allowed"],
        ["companion", "allowed", "pursuit", "health"],
        ["companion", "allowed", "--json", "pursuit", "health"],
        ["companion", "allowed", "--"],
        ["companion", "allowed", "--", "companion", "allowed", "--", "version"],
    ],
)
def test_companion_allowed_requires_one_bounded_target_after_separator(argv: list[str]) -> None:
    assert is_allowed(argv, "read", NO_ACT_POLICY) is False


def test_companion_allowed_does_not_grant_the_nested_target() -> None:
    query = ["companion", "allowed", "--", "sf", "set-next-steps", "006EXAMPLE", "text"]

    assert is_allowed(query, "read", NO_ACT_POLICY) is True
    assert is_allowed(query[3:], "read", NO_ACT_POLICY) is False


def test_companion_allowed_bounds_target_argv() -> None:
    assert is_allowed(["companion", "allowed", "--", *("x" for _ in range(65))], "read", NO_ACT_POLICY) is False
    assert is_allowed(["companion", "allowed", "--", "x" * 4097], "read", NO_ACT_POLICY) is False


def test_every_policy_path_is_a_real_cli_leaf() -> None:
    leaves = {tuple(node.full_name.split()) for node in walk_cli() if node.is_leaf}
    assert set(READ_ONLY_POLICIES) <= leaves


def test_every_reviewed_option_matches_the_click_leaf_name_and_arity() -> None:
    leaves = {tuple(node.full_name.split()): node.command for node in walk_cli() if node.is_leaf}
    for path, policy in READ_ONLY_POLICIES.items():
        click_options = {
            option: parameter
            for parameter in leaves[path].params
            if isinstance(parameter, click.Option)
            for option in parameter.opts
        }
        for option in policy.options:
            parameter = click_options[option]
            assert (0 if parameter.is_flag else parameter.nargs) == policy.options[option].arity


def test_every_positional_contract_matches_the_click_leaf() -> None:
    leaves = {tuple(node.full_name.split()): node.command for node in walk_cli() if node.is_leaf}
    for path, policy in READ_ONLY_POLICIES.items():
        click_arguments = [parameter for parameter in leaves[path].params if isinstance(parameter, click.Argument)]
        if policy.checked_remainder:
            assert len(click_arguments) == 1
            assert click_arguments[0].nargs == -1
            assert click_arguments[0].required is False
            continue
        assert len(click_arguments) == len(policy.positionals)
        assert [argument.required for argument in click_arguments] == [value.required for value in policy.positionals]


def test_every_click_option_is_explicitly_allowed_or_denied() -> None:
    leaves = {tuple(node.full_name.split()): node.command for node in walk_cli() if node.is_leaf}
    for path, policy in READ_ONLY_POLICIES.items():
        click_options = {
            option
            for parameter in leaves[path].params
            if isinstance(parameter, click.Option)
            for option in parameter.opts
        }
        classified = set(policy.options) | set(policy.denied_options)
        assert click_options == classified, f"unclassified read-tier option on {' '.join(path)}"
