"""Characterization tests pinning companion mapping tables to their upstream sources.

The mapping tables are string contracts (design D3). These tests diff them
against the authoritative registries so a new watcher or CLI group cannot be
added without the companion tables being updated deliberately.
"""

import click
import pytest

from fieldkit.__main__ import _COMMANDS
from fieldkit.cli_registry import walk_cli
from fieldkit.companion.mapping import (
    ACT_PREVIEW_DENIED_OPTIONS,
    ALERT_SKILL_MAP,
    DRY_RUN_CAPABLE,
    READ_ONLY_POLICIES,
    WATCHER_SEVERITY_MAP,
    suggested_skill_for,
)
from fieldkit.watch.constants import KNOWN_WATCHERS

pytestmark = [pytest.mark.unit, pytest.mark.characterization]

_SEVERITIES = frozenset({"critical", "warning", "info"})


def test_watcher_severity_map_covers_all_known_watchers() -> None:
    # Contract: every registered watcher has an explicit severity decision.
    result = set(WATCHER_SEVERITY_MAP) ^ set(KNOWN_WATCHERS)
    assert result == set(), (
        f"WATCHER_SEVERITY_MAP out of sync with watch/constants.py KNOWN_WATCHERS: {sorted(result)}. "
        "Add the new watcher to the map (choose a severity deliberately) or remove the stale entry."
    )


def test_watcher_severity_values_are_valid() -> None:
    result = {v for v in WATCHER_SEVERITY_MAP.values() if v not in _SEVERITIES}
    assert result == set()


def test_read_only_commands_reference_registered_groups() -> None:
    # Contract: every read-only entry names a group that exists in the dispatcher.
    result = {path[0] for path in READ_ONLY_POLICIES if path[0] not in _COMMANDS}
    assert result == set(), (
        f"READ_ONLY_POLICIES names unregistered CLI groups: {sorted(result)}. "
        "A renamed or removed group must be reflected here or the gate silently allows nothing."
    )


def test_read_only_commands_exclude_known_write_groups() -> None:
    # Only the genuine leaf command `version` is one token. Groups with write
    # subcommands must never be blessed by a group-prefix policy.
    result = {path for path in READ_ONLY_POLICIES if len(path) == 1 and path != ("version",)}
    assert result == set(), f"bare-group read-only entries beyond 'version': {sorted(result)}"


def test_dry_run_capable_names_registered_groups() -> None:
    # Contract: every entry's group token exists in the dispatcher.
    result = {entry.split()[0] for entry in DRY_RUN_CAPABLE if entry.split()[0] not in _COMMANDS}
    assert result == set(), (
        f"DRY_RUN_CAPABLE names unregistered CLI groups: {sorted(result)}. "
        "A renamed or removed group must be reflected here or act policy compilation silently "
        "rejects every entry naming it."
    )


def test_dry_run_capable_entries_have_full_command_paths() -> None:
    result = [entry for entry in DRY_RUN_CAPABLE if len(entry.split()) < 2]
    assert result == []


@pytest.mark.parametrize("entry", sorted(DRY_RUN_CAPABLE))
def test_dry_run_capable_entries_have_actual_preview_options(entry: str) -> None:
    nodes = walk_cli()
    assert nodes
    leaves = {
        node.full_name: node.command
        for node in nodes
        if node.is_leaf or (isinstance(node.command, click.Group) and node.command.invoke_without_command)
    }
    assert entry in leaves
    assert any(
        isinstance(parameter, click.Option) and "--dry-run" in parameter.opts for parameter in leaves[entry].params
    )


def test_act_preview_denied_options_name_real_reviewed_options() -> None:
    leaves = {
        node.full_name: node.command
        for node in walk_cli()
        if node.is_leaf or (isinstance(node.command, click.Group) and node.command.invoke_without_command)
    }

    assert set(ACT_PREVIEW_DENIED_OPTIONS) <= DRY_RUN_CAPABLE
    for entry, denied in ACT_PREVIEW_DENIED_OPTIONS.items():
        actual = {
            option
            for parameter in leaves[entry].params
            if isinstance(parameter, click.Option)
            for option in parameter.opts
        }
        assert denied
        assert denied <= actual


def test_dry_run_capable_excludes_unregistered_watch_subcommands() -> None:
    # "watch morning-brief" and "watch repair-transition-dates" are not real
    # two-token CLI invocations: morning-brief is only reachable via
    # `watch run --all`, and repair-transition-dates was renamed to
    # `pursuit repair-dates` (R25 — no alias left behind).
    assert "watch morning-brief" not in DRY_RUN_CAPABLE
    assert "watch repair-transition-dates" not in DRY_RUN_CAPABLE


def test_dry_run_capable_includes_registered_dry_run_commands() -> None:
    # brief generate and pursuit repair-dates each carry a
    # literal --dry-run Click option but were missing from the table.
    for entry in ("brief generate", "pursuit repair-dates"):
        assert entry in DRY_RUN_CAPABLE


def test_gtask_mutations_are_previewable_but_not_read_only() -> None:
    assert {"gtask create", "gtask complete"} <= DRY_RUN_CAPABLE
    assert not any(path[0] == "gtask" for path in READ_ONLY_POLICIES)


def test_alert_skill_map_values_are_nonempty_slugs() -> None:
    result = [v for v in ALERT_SKILL_MAP.values() if not v or " " in v]
    assert result == []


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("pursuit-stall", "grill"),
        ("pursuit-stall-alerts", "grill"),  # longest-prefix on full stem
        ("contract-expiry", "engagement-health"),
        ("draft-queue", "followup-draft"),
        ("no-such-alert", None),
    ],
)
def test_suggested_skill_for(stem: str, expected: str | None) -> None:
    result = suggested_skill_for(stem)
    assert result == expected
