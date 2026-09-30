"""Tests for scripts/generate_cli_docs.py — the in-process CLI reference generator.

Two properties matter and neither was previously covered:

1. The output must not depend on the environment that generated it. The
   subprocess-era generator inherited ``COLUMNS`` into every ``fieldkit --help``
   spawn, so running ``make docs`` in a narrow terminal committed a reflowed
   document — and the freshness gate then failed for everyone else, reporting
   staleness for a change that was nothing but line wrapping.
2. It must document the whole command tree. The old generator hard-coded three
   levels of nesting and silently omitted anything deeper.
"""

import os
import re

import pytest

from scripts import generate_cli_docs as generator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command,example",
    [("gmail decay", "acme-corp.example.com"), ("sf account", "acme-corp"), ("pursuit create", "acme-corp")],
)
def test_account_help_includes_documented_fictional_example(command: str, example: str) -> None:
    from fieldkit.cli_registry import walk_cli

    node = next(node for node in walk_cli() if node.full_name == command)
    content = generator.render_help(node)

    assert example in content


def test_every_generated_block_is_live_command_help() -> None:
    from fieldkit.cli_registry import walk_cli

    content = generator.generate()

    assert "## Common patterns" not in content
    assert "> **Auth:**" not in content
    blocks = re.findall(r"```[^\n]*\n(.*?)\n```", content, re.DOTALL)
    expected = [generator.render_help(node) for node in walk_cli(generator.command_groups())]
    assert blocks == expected


def test_generated_reference_header_has_no_calendar_dependency() -> None:
    content = generator.generate()

    assert "> Generated from the registered CLI command tree." in content
    assert not re.search(r"^> Auto-generated \d{4}-\d{2}-\d{2}", content, re.MULTILINE)


def test_new_registered_group_appears_without_copied_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(generator._COMMANDS_DICT, "sample", ("Sample command", "fieldkit.commands.version.cli"))

    content = generator.generate()

    assert "## `fieldkit sample`" in content


def test_output_is_independent_of_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """COLUMNS must not change a single byte of the generated reference."""
    monkeypatch.setenv("COLUMNS", "200")
    wide = generator.generate()
    monkeypatch.setenv("COLUMNS", "40")
    narrow = generator.generate()
    assert wide == narrow


def test_output_is_independent_of_columns_being_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "132")
    with_columns = generator.generate()
    monkeypatch.delenv("COLUMNS", raising=False)
    without_columns = generator.generate()
    assert with_columns == without_columns


def test_generated_doc_covers_every_leaf_command() -> None:
    """Every command the registry knows about gets a section.

    The registry is what agents route on; a command present there and absent
    here is documentation that disagrees with the machine-readable surface.
    """
    from fieldkit.cli_registry import build_registry

    content = generator.generate()
    missing = [e.full_name for e in build_registry() if f"`fieldkit {e.full_name}`" not in content]
    assert not missing, f"leaf commands absent from the generated reference: {missing}"


def test_generated_doc_covers_four_level_commands() -> None:
    """`watch run pursuit-stalls ack` is depth 3 — the case the old generator dropped."""
    content = generator.generate()
    assert "#### `fieldkit watch run pursuit-stalls`" in content
    assert "##### `fieldkit watch run pursuit-stalls ack`" in content


def test_usage_lines_are_prefixed_with_fieldkit() -> None:
    """A usage line reading `Usage: sf ...` would be uncopyable into a shell."""
    content = generator.generate()
    usage_lines = [line for line in content.splitlines() if line.startswith("Usage: ")]
    assert usage_lines
    bad = [line for line in usage_lines if not line.startswith("Usage: fieldkit")]
    assert not bad, f"usage lines missing the fieldkit prefix: {bad[:5]}"


def test_generator_spawns_no_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of implementation change: ~111 spawns become zero.

    Guarded rather than merely measured — a helper that quietly reintroduces a
    `fieldkit --help` shell-out would restore the ~25s runtime without failing
    anything else.
    """
    import subprocess

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"generator spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", _fail)
    monkeypatch.setattr(subprocess, "check_output", _fail)
    monkeypatch.setattr(subprocess, "Popen", _fail)
    monkeypatch.setattr(os, "system", _fail)

    generator.generate()
