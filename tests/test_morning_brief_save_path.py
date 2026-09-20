"""implementation change: brief save path is printed to stdout, not stderr.

Tests that the "Brief saved: ..." message emitted by _run() in
fieldkit/morning_brief/main.py goes to stdout (via click.echo) and is
absent from stderr.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.unit
def test_no_llm_save_path_on_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_run(no_llm=True) must print 'Brief saved: ...' to stdout, not stderr."""
    # Arrange: point data_root and fieldkit_root at tmp_path so no real FS is needed.
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()

    with (
        patch("fieldkit.commands.brief.main.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.brief.main.get_fieldkit_root", return_value=tmp_path),
        patch("fieldkit.commands.brief.main.collect_pursuit_alerts", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_champion_signals", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_decay_signals", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_stale_prose", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_tasks", return_value=("(none)", "(none)")),
        patch("fieldkit.commands.brief.main._collect_degraded_sources", return_value=[]),
        patch("fieldkit.commands.brief.main._render_degraded_section", return_value=""),
    ):
        from fieldkit.commands.brief.main import _run

        _run(no_llm=True, account=None)

    captured = capsys.readouterr()

    # The save-path message must appear on stdout.
    assert "Brief saved:" in captured.out, (
        f"Expected 'Brief saved:' on stdout, got stdout={captured.out!r}, stderr={captured.err!r}"
    )
    # It must NOT appear on stderr.
    assert "Brief saved:" not in captured.err, f"'Brief saved:' must not appear on stderr, got stderr={captured.err!r}"
    # The old stderr format must not appear anywhere.
    assert "Saved to:" not in captured.err, (
        f"Old 'Saved to:' format must not appear on stderr, got stderr={captured.err!r}"
    )


@pytest.mark.unit
def test_llm_save_path_on_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_run(no_llm=False) must print 'Brief saved: ...' to stdout, not stderr."""
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()

    with (
        patch("fieldkit.commands.brief.main.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.brief.main.get_fieldkit_root", return_value=tmp_path),
        patch("fieldkit.commands.brief.main.collect_pursuit_alerts", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_champion_signals", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_decay_signals", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_stale_prose", return_value="(none)"),
        patch("fieldkit.commands.brief.main.collect_tasks", return_value=("(none)", "(none)")),
        patch("fieldkit.commands.brief.main._collect_degraded_sources", return_value=[]),
        patch("fieldkit.commands.brief.main._render_degraded_section", return_value=""),
        # Stub LLM so no real API call is made.
        patch("fieldkit.commands.brief.main.synthesize", return_value="## LLM Brief\n\nStub output."),
    ):
        from fieldkit.commands.brief.main import _run

        _run(no_llm=False, account=None)

    captured = capsys.readouterr()

    assert "Brief saved:" in captured.out, (
        f"Expected 'Brief saved:' on stdout, got stdout={captured.out!r}, stderr={captured.err!r}"
    )
    assert "Brief saved:" not in captured.err, f"'Brief saved:' must not appear on stderr, got stderr={captured.err!r}"
    assert "Saved to:" not in captured.err, (
        f"Old 'Saved to:' format must not appear on stderr, got stderr={captured.err!r}"
    )
