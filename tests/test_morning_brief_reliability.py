"""Brief reliability: degraded synthesis, workspace output, and stale-prose collection.

LLM failures retain their category and include a [DEGRADED] marker in the saved brief.
Generation saves to data_root/briefs/ and exits 3 when configuration is absent.

Exit-code contract: _run() raises typed exceptions; cli_main() at the entry
point maps them to exit codes.  Tests that assert on exit codes wrap _run()
in cli_main() to exercise the full exception→exit-code mapping path.
"""

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.brief.pipeline_only as _brief_mod
from fieldkit.cli_exit import cli_main
from fieldkit.commands.brief.cli import _run_pipeline_only as _run
from fieldkit.config import ConfigError
from fieldkit.errors import LLMError


@pytest.fixture(autouse=True)
def configured_report_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_brief_mod, "get_llm_model", lambda: "vertex_ai/test-model")
    monkeypatch.setattr(_brief_mod, "llm_disabled", lambda: False)


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


# ── Provider failures retain their category and mark the persisted report ───────


@pytest.mark.unit
def test_general_llm_failure_exits_3(tmp_path: Path) -> None:
    """A general provider failure remains an invalid request/configuration failure."""
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        _enter_collector_patches(stack, data_root)
        stack.enter_context(patch.object(_brief_mod, "synthesize", side_effect=LLMError("test failure")))
        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)

    assert exc_info.value.code == 3, f"Expected exit code 3, got {exc_info.value.code}"


@pytest.mark.unit
def test_llm_failure_brief_contains_degraded_marker(tmp_path: Path) -> None:
    """Brief written on LLM failure must contain the [DEGRADED] marker.

    The brief must be saved to disk AND contain [DEGRADED] before the exception
    propagates through cli_main().
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
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
    """Successful LLM synthesis must exit 0 with no DEGRADED marker."""
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
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
    """--no-llm must save the brief to data_root/briefs/morning-brief-{today}.md."""
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
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
        pytest.raises(SystemExit) as exc_info,
        cli_main(),
    ):
        _run(no_llm=True, account=None)

    assert exc_info.value.code == 3, f"Expected exit code 3, got {exc_info.value.code}"

    # No brief file must have been written anywhere under tmp_path
    written = list(tmp_path.rglob("morning-brief-*.md"))
    assert not written, f"Expected no brief files written, found: {written}"


# ── Stale-prose collection ───────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_stale_prose_reports_no_findings(tmp_path: Path) -> None:
    """A real workspace pursuit without stale prose yields the empty-state message."""
    data_root = tmp_path / "data"
    pursuit_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(
        "---\nstage: propose\nsf_stage: Propose\n---\n# Deal\n",
        encoding="utf-8",
    )

    result = _brief_mod.collect_stale_prose(data_root)

    assert result == "None detected."


# ── implementation change: derived-doc caste marker on stored briefs ──────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("llm_fails", [False, True], ids=["no_llm_path", "degraded_path"])
def test_brief_file_carries_summary_caste_marker(tmp_path: Path, llm_fails: bool) -> None:
    """Every stored brief starts with the caste: summary provenance marker.

    On the degraded path the [DEGRADED] banner must remain
    the first body element after the frontmatter.
    """
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_brief_mod, "get_fieldkit_home", return_value=data_root))
        _enter_collector_patches(stack, data_root)
        if llm_fails:
            stack.enter_context(patch.object(_brief_mod, "synthesize", side_effect=LLMError("test failure")))
            with pytest.raises(SystemExit) as exc_info, cli_main():
                _run(no_llm=False, account=None)
            assert exc_info.value.code == 3
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
