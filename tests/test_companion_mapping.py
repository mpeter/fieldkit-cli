"""Characterization tests pinning companion mapping tables to their upstream sources.

The mapping tables are string contracts (design D3). These tests diff them
against the authoritative registries so a new watcher or CLI group cannot be
added without the companion tables being updated deliberately.
"""

import pytest

from fieldkit.__main__ import _COMMANDS
from fieldkit.companion.mapping import (
    ALERT_SKILL_MAP,
    DRY_RUN_CAPABLE,
    READ_ONLY_COMMANDS,
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
    result = {group for group, _sub in READ_ONLY_COMMANDS if group not in _COMMANDS}
    assert result == set(), (
        f"READ_ONLY_COMMANDS names unregistered CLI groups: {sorted(result)}. "
        "A renamed or removed group must be reflected here or the gate silently allows nothing."
    )


def test_read_only_commands_exclude_known_write_groups() -> None:
    # Groups whose subcommands mutate external state must never appear with
    # a None sub (which would bless the entire group as read-only).
    result = {group for group, sub in READ_ONLY_COMMANDS if sub is None and group != "version"}
    assert result == set(), f"bare-group read-only entries beyond 'version': {sorted(result)}"


def test_dry_run_capable_names_registered_groups() -> None:
    # Contract: every entry's group token exists in the dispatcher.
    result = {entry.split()[0] for entry in DRY_RUN_CAPABLE if entry.split()[0] not in _COMMANDS}
    assert result == set(), (
        f"DRY_RUN_CAPABLE names unregistered CLI groups: {sorted(result)}. "
        "A renamed or removed group must be reflected here or validate_allowlist silently "
        "rejects every entry naming it."
    )


def test_dry_run_capable_entries_are_two_tokens() -> None:
    # Contract: every entry is exactly "group subcommand" — validate_allowlist's
    # own token-count check means a single-token or three-token entry here can
    # never match a real allowlist entry, so it would be silent dead weight.
    result = [entry for entry in DRY_RUN_CAPABLE if len(entry.split()) != 2]
    assert result == []


def test_dry_run_capable_excludes_unregistered_watch_subcommands() -> None:
    # "watch morning-brief" and "watch repair-transition-dates" are not real
    # two-token CLI invocations: morning-brief is only reachable via
    # `watch run --all`, and repair-transition-dates was renamed to
    # `pursuit repair-dates` (R25 — no alias left behind).
    assert "watch morning-brief" not in DRY_RUN_CAPABLE
    assert "watch repair-transition-dates" not in DRY_RUN_CAPABLE


def test_dry_run_capable_includes_registered_dry_run_commands() -> None:
    # health run, brief generate, and pursuit repair-dates each carry a
    # literal --dry-run Click option but were missing from the table.
    for entry in ("health run", "brief generate", "pursuit repair-dates"):
        assert entry in DRY_RUN_CAPABLE


def test_gtask_mutations_are_previewable_but_not_read_only() -> None:
    assert {"gtask create", "gtask complete"} <= DRY_RUN_CAPABLE
    assert not any(group == "gtask" for group, _subcommand in READ_ONLY_COMMANDS)


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
