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

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_cli_docs.py"


def _load_generator():  # type: ignore[no-untyped-def]
    """Import the generator by path — scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("generate_cli_docs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_cli_docs"] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


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
