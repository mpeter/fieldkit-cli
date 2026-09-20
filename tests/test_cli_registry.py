"""Tests for fieldkit.cli_registry — the in-process Click introspection walker.

D1 Wave 5 (D4): `fieldkit commands --json` registry. Per the coverage
strategy in `openspec/changes/cli-ux-redesign/design.md`, every entry in
`_COMMANDS` must appear in the built registry with the required fields
(`full_name`, `summary`, `write_class`).
"""

import click
import pytest

from fieldkit.__main__ import _COMMANDS
from fieldkit.cli_registry import (
    CommandEntry,
    _classify_write,
    _leaf_entry,
    _parse_exit_code_notes,
    build_registry,
    declare_account_scope,
    declare_write,
    get_write_declaration,
    walk_cli,
)

pytestmark = pytest.mark.unit


def test_build_registry_covers_every_top_level_group() -> None:
    """Every _COMMANDS group contributes at least one leaf entry to the registry."""
    entries = build_registry()
    covered_prefixes = {e.full_name.split(" ", 1)[0] for e in entries}
    assert covered_prefixes == set(_COMMANDS), f"Groups missing from the registry: {set(_COMMANDS) - covered_prefixes}"


def test_build_registry_entries_have_required_fields() -> None:
    """Every entry has a non-empty full_name/summary and a valid write_class."""
    entries = build_registry()
    assert entries, "registry must not be empty"
    for e in entries:
        assert e.full_name
        assert isinstance(e.summary, str)
        assert e.write_class in {"read-only", "workspace", "external"}
        assert isinstance(e.account_scopable, bool)


def test_build_registry_full_names_are_unique() -> None:
    """No two leaf commands should collapse to the same full_name."""
    entries = build_registry()
    names = [e.full_name for e in entries]
    assert len(names) == len(set(names)), "duplicate full_name in registry"


def test_command_entry_to_dict_shape() -> None:
    """to_dict() emits the documented JSON shape for the registry contract."""
    entry = CommandEntry(full_name="sf listview", summary="Sync SF opportunities.", write_class="workspace")
    d = entry.to_dict()
    assert d["full_name"] == "sf listview"
    assert d["write_class"] == "workspace"
    assert d["arguments"] == []
    assert d["flags"] == []
    assert d["account_scopable"] is False
    assert d["exit_code_notes"] is None


@pytest.mark.parametrize(
    "flag_names,expected",
    [
        (set(), "read-only"),
        ({"--dry-run"}, "workspace"),
        ({"--confirm"}, "external"),
        ({"--confirm", "--dry-run"}, "external"),
    ],
)
def test_classify_write(flag_names: set[str], expected: str) -> None:
    assert _classify_write(flag_names) == expected


def test_parse_exit_code_notes_extracts_paragraph() -> None:
    help_text = """Does a thing.

    Exit codes: 0 success; 2 auth failure; 3 data error.

    More prose that must not be included.
    """
    notes = _parse_exit_code_notes(help_text)
    assert notes is not None
    assert notes.startswith("Exit codes:")
    assert "More prose" not in notes


def test_parse_exit_code_notes_absent() -> None:
    assert _parse_exit_code_notes("No exit code section here.") is None
    assert _parse_exit_code_notes(None) is None


# ---------------------------------------------------------------------------
# Write declarations (@declare_write)
# ---------------------------------------------------------------------------


def test_declared_write_class_overrides_flag_inference() -> None:
    """A declaration wins over the flag heuristic, and is marked as declared.

    This is the whole mechanism: the command below offers no write flags, so
    inference would call it "read-only". The declaration says otherwise, and
    the declaration is what the gate reads.
    """

    @declare_write("external")
    @click.command("danger")
    def cmd() -> None:
        pass

    entry = _leaf_entry(cmd, "test danger")
    assert entry.write_class == "external"
    assert entry.write_class_source == "declared"


def test_undeclared_command_falls_back_to_inference() -> None:
    @click.command("harmless")
    @click.option("--dry-run", is_flag=True)
    def cmd(dry_run: bool) -> None:
        pass

    entry = _leaf_entry(cmd, "test harmless")
    assert entry.write_class == "workspace"
    assert entry.write_class_source == "inferred"
    assert entry.confirm_exempt is None


def test_confirm_exempt_is_carried_into_the_entry() -> None:
    @declare_write("external", confirm_exempt="runs unattended")
    @click.command("auto")
    def cmd() -> None:
        pass

    entry = _leaf_entry(cmd, "test auto")
    assert entry.confirm_exempt == "runs unattended"


def test_declare_write_below_click_decorator_raises() -> None:
    """Applied in the wrong order it would annotate a bare function and vanish."""
    with pytest.raises(TypeError, match="above the Click command decorator"):

        @click.command("wrong")
        @declare_write("external")
        def cmd() -> None:
            pass


def test_confirm_exempt_on_non_external_declaration_raises() -> None:
    with pytest.raises(ValueError, match="only meaningful for external writes"):
        declare_write("workspace", confirm_exempt="nope")


def test_get_write_declaration_returns_none_for_undeclared() -> None:
    @click.command("plain")
    def cmd() -> None:
        pass

    assert get_write_declaration(cmd) is None


def test_to_dict_exposes_declaration_provenance() -> None:
    entry = CommandEntry(
        full_name="issue create",
        summary="Create an issue.",
        write_class="external",
        write_class_source="declared",
        confirm_exempt="unattended",
    )
    d = entry.to_dict()
    assert d["write_class"] == "external"
    assert d["write_class_source"] == "declared"
    assert d["confirm_exempt"] == "unattended"


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------


_GITHUB_MUTATING_ISSUE_COMMANDS = (
    "issue create",
    "issue close",
    "issue reopen",
    "issue fix",
    "issue plan",
    "issue edit",
    "issue note",
    "issue link",
    "issue sync-milestone",
)


@pytest.mark.parametrize("full_name", _GITHUB_MUTATING_ISSUE_COMMANDS)
def test_github_mutating_issue_commands_are_declared_external(full_name: str) -> None:
    """Each of these POSTs/PATCHes the GitHub REST API via GHIssueStore.

    Before declarations existed they all classified "read-only", because that
    is what flag inference reports for a command exposing neither --confirm nor
    --dry-run. Dropping a declaration would silently restore that, so pin it.
    """
    entries = {e.full_name: e for e in build_registry()}
    entry = entries[full_name]
    assert entry.write_class == "external"
    assert entry.write_class_source == "declared"
    assert entry.confirm_exempt, "an external write without --confirm must record why"


# ---------------------------------------------------------------------------
# walk_cli — the traversal shared with scripts/generate_cli_docs.py
# ---------------------------------------------------------------------------


def test_walk_cli_yields_groups_as_well_as_leaves() -> None:
    """The docs generator needs the group nodes the registry discards."""
    nodes = walk_cli()
    assert any(not n.is_leaf for n in nodes), "no group nodes in the walk"
    assert any(n.is_leaf for n in nodes), "no leaf nodes in the walk"


def test_build_registry_is_exactly_the_leaves_of_the_walk() -> None:
    """One mechanism, two consumers — the registry must not walk separately.

    Pinning this is the point: a private traversal in either consumer drifts
    from the other silently. That is not hypothetical — the subprocess-era docs
    generator hard-coded three levels of nesting and so never documented
    `watch run pursuit-stalls ack`, which the registry had listed all along.
    """
    walk_leaves = [n.full_name for n in walk_cli() if n.is_leaf]
    registry_names = [e.full_name for e in build_registry()]
    assert registry_names == walk_leaves


def test_walk_cli_respects_requested_group_order() -> None:
    """The docs generator passes its display order; the registry takes the default."""
    nodes = walk_cli(["version", "auth"])
    top_level = [n.full_name for n in nodes if n.depth == 0]
    assert top_level == ["version", "auth"]


def test_walk_cli_ignores_unknown_group_names() -> None:
    assert walk_cli(["not-a-real-group"]) == []


def test_walk_cli_depth_matches_name_segments() -> None:
    """Depth drives the doc heading level, so it must track the command path."""
    for node in walk_cli():
        assert node.depth == node.full_name.count(" ")


def test_walk_reaches_four_level_commands() -> None:
    """`watch run pursuit-stalls ack` is depth 3 — the level the old docs missed."""
    names = {n.full_name for n in walk_cli()}
    assert "watch run pursuit-stalls ack" in names


def test_context_carries_the_command_context_settings() -> None:
    """Help width comes from context_settings; a bare click.Context drops them.

    Dropping them renders help at a different width than the command's real
    --help, which is a silent documentation defect rather than a crash.
    """
    group_nodes = [n for n in walk_cli(["sf"]) if n.depth == 0]
    assert group_nodes
    assert group_nodes[0].ctx.max_content_width == 100


def test_top_level_group_usage_is_prefixed_with_fieldkit() -> None:
    """Usage lines must read `fieldkit sf`, not `sf` — Click derives it from parents."""
    group_nodes = [n for n in walk_cli(["sf"]) if n.depth == 0]
    assert group_nodes[0].ctx.command_path == "fieldkit sf"


# ---------------------------------------------------------------------------
# account_scope — filter vs selector
# ---------------------------------------------------------------------------


def test_optional_account_is_a_filter() -> None:
    @click.command("sweep")
    @click.option("--account", default=None)
    def cmd(account: str | None) -> None:
        pass

    assert _leaf_entry(cmd, "test sweep").account_scope == "filter"


def test_required_account_is_a_selector() -> None:
    """Mandatory --account names a target; it cannot narrow anything."""

    @click.command("make")
    @click.option("--account", required=True)
    def cmd(account: str) -> None:
        pass

    assert _leaf_entry(cmd, "test make").account_scope == "selector"


def test_no_account_option_yields_no_scope() -> None:
    @click.command("plain")
    def cmd() -> None:
        pass

    assert _leaf_entry(cmd, "test plain").account_scope is None


def test_declaration_overrides_requiredness() -> None:
    """`pursuit advance` is the live case: optional, but still a selector."""

    @declare_account_scope("selector")
    @click.command("advance")
    @click.option("--account", default=None)
    def cmd(account: str | None) -> None:
        pass

    assert _leaf_entry(cmd, "test advance").account_scope == "selector"


def test_declare_account_scope_below_click_decorator_raises() -> None:
    with pytest.raises(TypeError, match="above the Click command decorator"):

        @click.command("wrong")
        @declare_account_scope("filter")
        def cmd() -> None:
            pass


@pytest.mark.parametrize(
    "full_name",
    ["pursuit create", "pursuit archive", "pursuit rename", "pursuit advance"],
)
def test_pursuit_account_options_are_selectors_not_filters(full_name: str) -> None:
    """These name which pursuit to act on. An agent must not read them as filters.

    `account_scopable` alone says "has --account" and cannot express that
    passing it here changes the target rather than narrowing a sweep.
    """
    entries = {e.full_name: e for e in build_registry()}
    assert entries[full_name].account_scope == "selector"


def test_account_scope_is_set_for_every_command_with_the_option() -> None:
    for entry in build_registry():
        if entry.account_scopable:
            assert entry.account_scope in {"filter", "selector"}, entry.full_name
        else:
            assert entry.account_scope is None, entry.full_name
