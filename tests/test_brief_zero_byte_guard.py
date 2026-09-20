"""Tests for Spec 046: 0-byte brief file guard (implementation note).

Verifies that:
- After write_text, a 0-byte file raises RuntimeError and is deleted.
- A successful (non-empty) write completes without error.
- The morning-brief watcher logs a WARNING for pre-existing 0-byte stubs.
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# brief/main.py — _run() write sites
# ---------------------------------------------------------------------------


def _make_zero_write(output_path: Path) -> None:
    """Side-effect helper: write_text writes nothing (simulates empty content)."""
    output_path.write_text("", encoding="utf-8")


@pytest.mark.unit
def test_brief_writer_no_llm_raises_on_zero_byte_output(tmp_path: Path) -> None:
    """_run(no_llm=True): 0-byte write raises RuntimeError and deletes the stub."""
    from datetime import UTC, datetime

    from fieldkit.commands.brief import main as brief_main

    output_dir = tmp_path / "briefs"
    output_dir.mkdir()
    # Compute the expected output path using the same logic as the production code:
    # brief/main.py uses datetime.now(tz=UTC).date().isoformat() for the filename.
    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"

    # Patch _render_no_llm_brief to return an empty string so write_text
    # produces a 0-byte file, triggering the guard.
    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
        patch.object(brief_main, "get_fieldkit_root", return_value=tmp_path),
        patch.object(brief_main, "collect_pursuit_alerts", return_value="alerts"),
        patch.object(brief_main, "collect_champion_signals", return_value="signals"),
        patch.object(brief_main, "collect_decay_signals", return_value="decay"),
        patch.object(brief_main, "collect_stale_prose", return_value="none"),
        patch.object(brief_main, "collect_tasks", return_value=("today", "waiting")),
        patch.object(brief_main, "_collect_degraded_sources", return_value=[]),
        patch.object(brief_main, "_render_degraded_section", return_value=""),
        # Return empty string → write_text produces 0-byte file
        patch.object(brief_main, "_render_no_llm_brief", return_value=""),
        patch("builtins.print"),
        pytest.raises(RuntimeError, match="File written as 0 bytes"),
    ):
        brief_main._run(no_llm=True, account=None)

    # The 0-byte stub must have been deleted by the guard — not merely absent
    assert not output_path.exists(), f"0-byte stub {output_path} was not deleted after RuntimeError"


@pytest.mark.unit
def test_brief_writer_no_llm_happy_path(tmp_path: Path) -> None:
    """_run(no_llm=True): non-empty write completes without error."""
    from fieldkit.commands.brief import main as brief_main

    output_dir = tmp_path / "briefs"
    output_dir.mkdir()

    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
        patch.object(brief_main, "get_fieldkit_root", return_value=tmp_path),
        patch.object(brief_main, "collect_pursuit_alerts", return_value="alerts"),
        patch.object(brief_main, "collect_champion_signals", return_value="signals"),
        patch.object(brief_main, "collect_decay_signals", return_value="decay"),
        patch.object(brief_main, "collect_stale_prose", return_value="none"),
        patch.object(brief_main, "collect_tasks", return_value=("today", "waiting")),
        patch.object(brief_main, "_collect_degraded_sources", return_value=[]),
        patch.object(brief_main, "_render_degraded_section", return_value=""),
        patch.object(brief_main, "_render_no_llm_brief", return_value="# Brief\n\nContent here.\n"),
        patch("builtins.print"),
        patch("click.echo"),
    ):
        # Should not raise
        brief_main._run(no_llm=True, account=None)

    # File should exist and be non-empty
    from datetime import UTC, datetime

    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"
    assert output_path.exists()
    assert output_path.stat().st_size > 0


@pytest.mark.unit
def test_pipeline_only_json_emits_written_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.brief import main as brief_main

    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
        patch.object(brief_main, "collect_pursuit_alerts", return_value="alerts"),
        patch.object(brief_main, "collect_champion_signals", return_value="signals"),
        patch.object(brief_main, "collect_decay_signals", return_value="decay"),
        patch.object(brief_main, "collect_stale_prose", return_value="none"),
        patch.object(brief_main, "collect_tasks", return_value=("today", "waiting")),
        patch.object(brief_main, "_collect_degraded_sources", return_value=[]),
        patch.object(brief_main, "_render_degraded_section", return_value=""),
        patch.object(brief_main, "_render_no_llm_brief", return_value="# Brief\n"),
    ):
        brief_main._run(no_llm=True, account=None, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is True
    assert payload["path"].endswith(".md")


# ---------------------------------------------------------------------------
# morning_brief.py — _write_brief_to_disk() write site
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_brief_to_disk_raises_on_zero_byte(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_write_brief_to_disk(): 0-byte write raises RuntimeError and deletes stub."""
    from datetime import date

    import fieldkit.watch.morning_brief as mb_mod

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        patch.object(mb_mod, "get_watchers_dir", return_value=watchers_dir),
        patch.object(mb_mod, "write_run_status"),
        # Empty string → write_text produces 0-byte file
        pytest.raises(RuntimeError, match="File written as 0 bytes"),
    ):
        mb_mod._write_brief_to_disk(
            brief_md="",
            target_date=date(2026, 1, 1),
            elapsed=0.1,
            sources=[],
            dry_run=False,
        )

    # The 0-byte stub must be deleted
    stub = watchers_dir / "morning-brief-2026-01-01.md"
    assert not stub.exists(), "0-byte stub was not deleted after RuntimeError"


@pytest.mark.unit
def test_write_brief_to_disk_happy_path(tmp_path: Path) -> None:
    """_write_brief_to_disk(): non-empty write returns 0 and leaves file on disk."""
    from datetime import date

    import fieldkit.watch.morning_brief as mb_mod

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        patch.object(mb_mod, "get_watchers_dir", return_value=watchers_dir),
        patch.object(mb_mod, "write_run_status"),
    ):
        result = mb_mod._write_brief_to_disk(
            brief_md="# Morning Brief\n\nContent.\n",
            target_date=date(2026, 1, 1),
            elapsed=0.1,
            sources=[],
            dry_run=False,
        )

    assert result == 0
    stub = watchers_dir / "morning-brief-2026-01-01.md"
    assert stub.exists()
    assert stub.stat().st_size > 0


@pytest.mark.unit
def test_write_brief_to_disk_warns_on_existing_zero_byte_stubs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_write_brief_to_disk(): logs WARNING for pre-existing 0-byte stubs before writing."""
    from datetime import date

    import fieldkit.watch.morning_brief as mb_mod

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    # Create a pre-existing 0-byte stub for a different date
    stub = watchers_dir / "morning-brief-2026-05-31.md"
    stub.write_text("", encoding="utf-8")
    assert stub.stat().st_size == 0

    with (
        patch.object(mb_mod, "get_watchers_dir", return_value=watchers_dir),
        patch.object(mb_mod, "write_run_status"),
        caplog.at_level(logging.WARNING, logger="fieldkit.watch"),
    ):
        result = mb_mod._write_brief_to_disk(
            brief_md="# Morning Brief\n\nContent.\n",
            target_date=date(2026, 1, 1),
            elapsed=0.1,
            sources=[],
            dry_run=False,
        )

    assert result == 0
    assert any("morning-brief-2026-05-31.md" in r.message for r in caplog.records), (
        "Expected WARNING about pre-existing 0-byte stub"
    )


# ---------------------------------------------------------------------------
# fieldkit.util.atomic — assert_nonzero_write() direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_assert_nonzero_write_happy_path(tmp_path: Path) -> None:
    """Non-empty file: assert_nonzero_write raises nothing and file is untouched."""
    from fieldkit.util.atomic import assert_nonzero_write

    f = tmp_path / "output.md"
    f.write_text("content", encoding="utf-8")
    assert_nonzero_write(f)  # must not raise
    assert f.exists()


@pytest.mark.unit
def test_assert_nonzero_write_zero_byte(tmp_path: Path) -> None:
    """0-byte file: assert_nonzero_write raises RuntimeError and deletes the file."""
    from fieldkit.util.atomic import assert_nonzero_write

    f = tmp_path / "output.md"
    f.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="File written as 0 bytes"):
        assert_nonzero_write(f)
    assert not f.exists()
