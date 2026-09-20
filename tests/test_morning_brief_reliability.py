"""Reliability tests for historic regression, historic regression, and implementation change.

historic regression: LLM failure must exit 1 and include a [DEGRADED] marker in the brief.
historic regression: --no-llm must save to data_root/briefs/; exits 2 when config is absent.
implementation change: collect_stale_prose() must resolve the script via fieldkit/, not lib/.

Exit-code contract: _run() raises typed exceptions; cli_main() at the entry
point maps them to exit codes.  Tests that assert on exit codes wrap _run()
in cli_main() to exercise the full exception→exit-code mapping path.
"""

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.commands.brief.main as _brief_mod
from fieldkit.cli_exit import cli_main
from fieldkit.commands.brief.main import _run
from fieldkit.config import ConfigError
from fieldkit.errors import LLMError


def _enter_collector_patches(stack: contextlib.ExitStack, data_root: Path) -> None:
    """Enter all collector stub patches into the given ExitStack."""
    stack.enter_context(
        patch.object(_brief_mod, "collect_pursuit_alerts", return_value="No active pursuits flagged today.")
    )
    stack.enter_context(
        patch.object(_brief_mod, "collect_champion_signals", return_value="No champion signal data available.")
    )
    stack.enter_context(patch.object(_brief_mod, "collect_decay_signals", return_value="No decay data available."))
    stack.enter_context(patch.object(_brief_mod, "collect_stale_prose", return_value="None detected."))
    stack.enter_context(
        patch.object(
            _brief_mod,
            "collect_tasks",
            return_value=("(nothing committed for today yet)", "(nothing in the queue)"),
        )
    )
    stack.enter_context(patch.object(_brief_mod, "_collect_degraded_sources", return_value=[]))
    stack.enter_context(patch.object(_brief_mod, "_render_degraded_section", return_value=""))


# ── historic regression: LLM failure exits 1 with DEGRADED marker ────────────────────────


@pytest.mark.unit
def test_llm_failure_exits_1(tmp_path: Path) -> None:
    """_run(no_llm=False) must cause exit code 1 when synthesize() raises LLMError.

    _run() raises LLMError(category="rate-limit"); cli_main() maps it to
    EXIT_PARTIAL (1).  The test wraps _run() in cli_main() to exercise the
    full exception→exit-code mapping path.

    Spec ref: brief-reliability/spec.md — Scenario: LLM synthesis raises LLMError.
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        stack.enter_context(patch.object(_brief_mod, "synthesize", side_effect=LLMError("test failure")))
        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)

    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"


@pytest.mark.unit
def test_llm_failure_brief_contains_degraded_marker(tmp_path: Path) -> None:
    """Brief written on LLM failure must contain the [DEGRADED] marker.

    Spec ref: brief-reliability/spec.md — Scenario: LLM synthesis raises LLMError.
    The brief must be saved to disk AND contain [DEGRADED] before the exception
    propagates through cli_main().
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        stack.enter_context(patch.object(_brief_mod, "synthesize", side_effect=LLMError("test failure")))
        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)
        assert exc_info.value.code != 0

    # Brief must have been written before the exception propagated
    briefs_dir = data_root / "briefs"
    written = sorted(briefs_dir.glob("morning-brief-*.md"))
    assert written, "Expected brief file to be written before exception propagated"

    content = written[0].read_text(encoding="utf-8")
    assert "[DEGRADED]" in content, f"Expected '[DEGRADED]' in brief content, got:\n{content[:500]}"


@pytest.mark.unit
def test_llm_success_exits_0(tmp_path: Path) -> None:
    """Successful LLM synthesis must exit 0 with no DEGRADED marker.

    Spec ref: brief-reliability/spec.md — Scenario: Successful LLM synthesis exits 0.
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        stack.enter_context(patch.object(_brief_mod, "synthesize", return_value="## Morning Brief\n\nAll good."))
        # Must not raise on success — no cli_main() wrapper needed
        _run(no_llm=False, account=None)

    briefs_dir = data_root / "briefs"
    written = sorted(briefs_dir.glob("morning-brief-*.md"))
    assert written, "Expected brief file to be written on success"

    content = written[0].read_text(encoding="utf-8")
    assert "[DEGRADED]" not in content, f"[DEGRADED] must not appear in successful brief:\n{content[:500]}"


# ── historic regression: --no-llm saves to data_root/briefs/ ─────────────────────────────


@pytest.mark.unit
def test_no_llm_saves_to_data_root(tmp_path: Path) -> None:
    """--no-llm must save the brief to data_root/briefs/morning-brief-{today}.md.

    Spec ref: brief-reliability/spec.md — Scenario: --no-llm saves to data_root/briefs/.
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        _run(no_llm=True, account=None)

    expected = data_root / "briefs" / f"morning-brief-{datetime.now(tz=UTC).date().isoformat()}.md"
    assert expected.exists(), (
        f"Expected brief at {expected} but it was not found. "
        f"Contents of {data_root / 'briefs'}: {list((data_root / 'briefs').glob('*'))}"
    )


@pytest.mark.unit
def test_no_llm_dry_run_does_not_create_a_brief_directory(tmp_path: Path) -> None:
    """The pipeline-only dry run renders output without writing workspace state."""
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        _run(no_llm=True, account=None, dry_run=True)

    assert not (data_root / "briefs").exists()


@pytest.mark.unit
def test_no_llm_config_error_exits_3(tmp_path: Path) -> None:
    """--no-llm must exit 3 when get_fieldkit_home() raises ConfigError.

    _run() raises ConfigError; cli_main() maps it to EXIT_DATA (3).
    The test wraps _run() in cli_main() to exercise the full mapping path.

    Spec ref: cli-exit-taxonomy — configuration failures use the data-error exit.
    No brief file must be written to any location.
    """
    with (
        patch.object(_brief_mod, "get_fieldkit_home", side_effect=ConfigError("no config")),
        patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path),
        pytest.raises(SystemExit) as exc_info,
        cli_main(),
    ):
        _run(no_llm=True, account=None)

    assert exc_info.value.code == 3, f"Expected exit code 3, got {exc_info.value.code}"

    # No brief file must have been written anywhere under tmp_path
    written = list(tmp_path.rglob("morning-brief-*.md"))
    assert not written, f"Expected no brief files written, found: {written}"


# ── implementation change: collect_stale_prose resolves via fieldkit/ path ─────────────────────


@pytest.mark.unit
def test_collect_stale_prose_finds_script_via_aeos_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """collect_stale_prose must find the script at fieldkit/pursuit/stale.py.

    Monkeypatches __file__ so the path derivation resolves to
    tmp_path/fieldkit/pursuit/stale.py.  The stub script prints
    "No stale prose contradictions found." and the function must return
    "None detected.".

    Spec ref: brief-reliability/spec.md — Scenario: Script found via fieldkit/ path.
    """
    # Fake package layout:
    #   tmp_path/fieldkit/morning_brief/main.py  <- monkeypatched __file__
    #   tmp_path/fieldkit/pursuit/stale.py           <- stub script
    fake_module_file = tmp_path / "fieldkit" / "morning_brief" / "main.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.touch()

    fake_script = tmp_path / "fieldkit" / "pursuit" / "stale.py"
    fake_script.parent.mkdir(parents=True, exist_ok=True)
    fake_script.write_text(
        'import sys\nprint("No stale prose contradictions found.")\nsys.exit(0)\n',
        encoding="utf-8",
    )

    # Minimal data_root with one pursuit so iterate_pursuits returns a path
    data_root = tmp_path / "data"
    pursuit_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(
        "---\nstage: propose\nsf_stage: Propose\n---\n# Deal\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_brief_mod, "__file__", str(fake_module_file))

    result = _brief_mod.collect_stale_prose(data_root)

    assert result == "None detected.", (
        f"Expected 'None detected.' when script prints the standard no-issues message, got: {result!r}"
    )


# ── implementation change: derived-doc caste marker on stored briefs ──────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("llm_fails", [False, True], ids=["no_llm_path", "degraded_path"])
def test_brief_file_carries_summary_caste_marker(tmp_path: Path, llm_fails: bool) -> None:
    """Every stored brief starts with the caste: summary provenance marker.

    Spec ref: doc-provenance/spec.md — Scenario: Morning brief is stamped on
    every write path. On the degraded path the [DEGRADED] banner must remain
    the first body element after the frontmatter (historic regression contract preserved).
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_root", return_value=tmp_path))
        _enter_collector_patches(stack, data_root)
        if llm_fails:
            stack.enter_context(patch.object(_brief_mod, "synthesize", side_effect=LLMError("test failure")))
            with pytest.raises(SystemExit) as exc_info, cli_main():
                _run(no_llm=False, account=None)
            assert exc_info.value.code == 1
        else:
            _run(no_llm=True, account=None)

    written = sorted((data_root / "briefs").glob("morning-brief-*.md"))
    assert written, "Expected brief file to be written"
    content = written[0].read_text(encoding="utf-8")

    assert content.startswith("---\ncaste: summary\n"), f"Missing caste marker, got:\n{content[:200]}"
    assert "generated_by: fieldkit brief generate --pipeline-only" in content
    assert "derived_from:" in content
    if llm_fails:
        body = content.split("---\n", 2)[2]
        assert body.lstrip("\n").startswith("> [DEGRADED]"), (
            f"[DEGRADED] banner must be the first body element after frontmatter, got:\n{body[:200]}"
        )
