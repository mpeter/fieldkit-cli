"""Tests for Spec 046: 0-byte brief file guard (implementation note).

Verifies that:
- After a write, zero bytes raise EmptyOutputError with factual cleanup metadata.
- A successful (non-empty) write completes without error.
- The morning-brief watcher logs a WARNING for pre-existing 0-byte stubs.
"""

import json
import logging
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.commands.brief.cli as brief_cli
from fieldkit.errors import EmptyOutputError, LLMErrorCategory


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", " \n\t"])
@pytest.mark.parametrize("no_llm", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("prior", ["missing", "directory", "report"])
@pytest.mark.parametrize("cli_boundary", [False, True])
def test_real_brief_empty_render_has_no_publication_or_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    no_llm: bool,
    dry_run: bool,
    prior: str,
    cli_boundary: bool,
) -> None:
    from datetime import UTC, datetime

    from click.testing import CliRunner

    from fieldkit.brief import pipeline_only as brief_main
    from fieldkit.commands.brief.cli import cli
    from fieldkit.errors import LLMError
    from fieldkit.util.atomic import atomic_text_write

    directory = tmp_path / "briefs"
    target = directory / f"morning-brief-{datetime.now(tz=UTC).date().isoformat()}.md"
    if prior != "missing":
        directory.mkdir()
        (directory / "other.md").write_bytes(b"unrelated")
    if prior == "report":
        target.write_bytes(b"prior brief")
        target.chmod(0o640)
    before = {path.name: (path.read_bytes(), path.stat().st_mode) for path in directory.glob("*")}
    monkeypatch.setattr(brief_main, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(brief_main, "get_llm_model", lambda: "vertex_ai/test-model")
    monkeypatch.setattr(brief_main, "llm_disabled", lambda: False)
    for name in ("collect_pursuit_alerts", "collect_champion_signals", "collect_decay_signals", "collect_stale_prose"):
        monkeypatch.setattr(brief_main, name, lambda *_args, **_kwargs: "data")
    monkeypatch.setattr(brief_main, "collect_tasks", lambda *_args, **_kwargs: ("today", "waiting"))
    monkeypatch.setattr(brief_main, "_collect_degraded_sources", lambda *_args: [])
    monkeypatch.setattr(brief_main, "_render_no_llm_brief", lambda **_kwargs: body)
    with (
        patch.object(brief_main, "synthesize", side_effect=LLMError("provider failed", category="auth")),
        patch.object(brief_main, "atomic_text_write", wraps=atomic_text_write) as writer,
        patch.object(brief_cli, "_emit_pipeline_only_result", wraps=brief_cli._emit_pipeline_only_result) as emitter,
    ):
        if cli_boundary:
            result = CliRunner().invoke(
                cli,
                [
                    "generate",
                    "--pipeline-only",
                    "--json",
                    *(["--no-llm"] if no_llm else []),
                    *(["--dry-run"] if dry_run else []),
                ],
            )
            assert result.exit_code == 3
            assert result.output.endswith("[cli_exit] Empty output detected — investigate before retrying.\n")
            assert "written" not in result.output
            assert "Brief saved" not in result.output
            assert "provider failed" not in result.output
            assert "Traceback" not in result.output
        else:
            with pytest.raises(EmptyOutputError, match="Empty output detected"):
                brief_cli._run_pipeline_only(no_llm=no_llm, account=None, dry_run=dry_run, as_json=True)
    writer.assert_not_called()
    emitter.assert_not_called()
    assert directory.exists() is (prior != "missing")
    assert {path.name: (path.read_bytes(), path.stat().st_mode) for path in directory.glob("*")} == before


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", " \n\t"])
@pytest.mark.parametrize("existing", [False, True])
def test_empty_body_preserves_prior_report(tmp_path: Path, body: str, existing: bool) -> None:
    from fieldkit.brief import pipeline_only as brief_main
    from fieldkit.errors import EmptyOutputError

    target = tmp_path / "report.md"
    if existing:
        target.write_bytes(b"prior report")
        target.chmod(0o640)
    with (
        patch.object(brief_main, "atomic_text_write") as writer,
        pytest.raises(EmptyOutputError, match="Empty output"),
    ):
        brief_main._write_brief(target, body)
    writer.assert_not_called()
    assert list(tmp_path.iterdir()) == ([target] if existing else [])
    if existing:
        assert target.read_bytes() == b"prior report"
        assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.unit
@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_zero_byte_anomaly_records_actual_cleanup(tmp_path: Path, cleanup_failed: bool) -> None:
    from fieldkit.errors import EmptyOutputError
    from fieldkit.util.atomic import assert_nonzero_write

    target = tmp_path / "report.md"
    target.write_bytes(b"")
    with (
        patch.object(Path, "unlink", side_effect=OSError("private cause")) if cleanup_failed else nullcontext(),
        pytest.raises(EmptyOutputError, match="Empty output") as caught,
    ):
        assert_nonzero_write(target)
    assert caught.value.cleanup_failed is cleanup_failed
    assert target.exists() is cleanup_failed
    if cleanup_failed:
        assert target.read_bytes() == b""


@pytest.mark.unit
@pytest.mark.parametrize("invalid", [0, 1, None, "true"])
def test_empty_output_cleanup_fact_requires_exact_bool(invalid: object) -> None:
    from fieldkit.errors import EmptyOutputError

    with pytest.raises(ValueError, match="cleanup_failed must be a bool"):
        EmptyOutputError(cleanup_failed=invalid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# brief/main.py — _run() write sites
# ---------------------------------------------------------------------------


def _make_zero_write(output_path: Path) -> None:
    """Side-effect helper: write_text writes nothing (simulates empty content)."""
    output_path.write_text("", encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("category", ["auth", "rate-limit", "general"])
def test_valid_brief_fallback_preserves_provider_error_and_preview(
    tmp_path: Path, category: LLMErrorCategory, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.brief import pipeline_only as brief_main
    from fieldkit.errors import LLMError

    error = LLMError("provider failed", category=category)
    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
        patch.object(brief_main, "get_llm_model", return_value="vertex_ai/test-model"),
        patch.object(brief_main, "llm_disabled", return_value=False),
        patch.object(brief_main, "collect_pursuit_alerts", return_value="alerts"),
        patch.object(brief_main, "collect_champion_signals", return_value="signals"),
        patch.object(brief_main, "collect_decay_signals", return_value="decay"),
        patch.object(brief_main, "collect_stale_prose", return_value="none"),
        patch.object(brief_main, "collect_tasks", return_value=("today", "waiting")),
        patch.object(brief_main, "_collect_degraded_sources", return_value=[]),
        patch.object(brief_main, "_render_no_llm_brief", return_value="# Valid brief\n"),
        patch.object(brief_main, "synthesize", side_effect=error),
        pytest.raises(LLMError, match="provider failed") as caught,
    ):
        brief_cli._run_pipeline_only(no_llm=False, account=None, dry_run=False, as_json=True)
    assert caught.value is error
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is True
    assert payload["degraded"] is True
    report = next((tmp_path / "briefs").iterdir())
    assert "# Valid brief" in report.read_text(encoding="utf-8")


@pytest.mark.unit
def test_brief_writer_no_llm_rejects_empty_body_before_publication(tmp_path: Path) -> None:
    """An empty rendered body cannot be masked by provenance or published as a stub."""
    from datetime import UTC, datetime

    from fieldkit.brief import pipeline_only as brief_main

    output_dir = tmp_path / "briefs"
    output_dir.mkdir()
    # Compute the expected output path using the same logic as the production code:
    # brief/main.py uses datetime.now(tz=UTC).date().isoformat() for the filename.
    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"

    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
        patch.object(brief_main, "collect_pursuit_alerts", return_value="alerts"),
        patch.object(brief_main, "collect_champion_signals", return_value="signals"),
        patch.object(brief_main, "collect_decay_signals", return_value="decay"),
        patch.object(brief_main, "collect_stale_prose", return_value="none"),
        patch.object(brief_main, "collect_tasks", return_value=("today", "waiting")),
        patch.object(brief_main, "_collect_degraded_sources", return_value=[]),
        patch.object(brief_main, "_render_degraded_section", return_value=""),
        patch.object(brief_main, "_render_no_llm_brief", return_value=""),
        patch("builtins.print"),
        pytest.raises(EmptyOutputError, match="Empty output detected"),
    ):
        brief_cli._run_pipeline_only(no_llm=True, account=None)

    assert not output_path.exists(), "empty body unexpectedly published a report"


@pytest.mark.unit
def test_brief_writer_no_llm_happy_path(tmp_path: Path) -> None:
    """_run(no_llm=True): non-empty write completes without error."""
    from fieldkit.brief import pipeline_only as brief_main

    output_dir = tmp_path / "briefs"
    output_dir.mkdir()

    with (
        patch.object(brief_main, "get_fieldkit_home", return_value=tmp_path),
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
        brief_cli._run_pipeline_only(no_llm=True, account=None)

    # File should exist and be non-empty
    from datetime import UTC, datetime

    today_str = datetime.now(tz=UTC).date().isoformat()
    output_path = output_dir / f"morning-brief-{today_str}.md"
    assert output_path.exists()
    assert output_path.stat().st_size > 0


@pytest.mark.unit
def test_pipeline_only_json_emits_written_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.brief import pipeline_only as brief_main

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
        brief_cli._run_pipeline_only(no_llm=True, account=None, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is True
    assert payload["path"].endswith(".md")


# ---------------------------------------------------------------------------
# morning_brief.py — _write_brief_to_disk() write site
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_brief_to_disk_raises_on_zero_byte(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_write_brief_to_disk(): empty output raises the canonical typed error."""
    from datetime import date

    import fieldkit.watch.morning_brief as mb_mod

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        patch.object(mb_mod, "get_watchers_dir", return_value=watchers_dir),
        patch.object(mb_mod, "write_run_status", return_value="written"),
        # Empty string → write_text produces 0-byte file
        pytest.raises(EmptyOutputError, match="Empty output detected"),
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
    assert not stub.exists(), "empty output unexpectedly left a stub"


@pytest.mark.unit
def test_write_brief_to_disk_happy_path(tmp_path: Path) -> None:
    """A nonempty publication succeeds and leaves the report on disk."""
    from datetime import date

    import fieldkit.watch.morning_brief as mb_mod

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        patch.object(mb_mod, "get_watchers_dir", return_value=watchers_dir),
        patch.object(mb_mod, "write_run_status", return_value="written"),
    ):
        result = mb_mod._write_brief_to_disk(
            brief_md="# Morning Brief\n\nContent.\n",
            target_date=date(2026, 1, 1),
            elapsed=0.1,
            sources=[],
            dry_run=False,
        )

    assert result.written is True
    assert result.run.exit_code == 0
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
        patch.object(mb_mod, "write_run_status", return_value="written"),
        caplog.at_level(logging.WARNING, logger="fieldkit.watch"),
    ):
        result = mb_mod._write_brief_to_disk(
            brief_md="# Morning Brief\n\nContent.\n",
            target_date=date(2026, 1, 1),
            elapsed=0.1,
            sources=[],
            dry_run=False,
        )

    assert result.written is True
    assert result.run.exit_code == 0
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
    result = assert_nonzero_write(f)
    assert result is None
    assert f.read_bytes() == b"content"
    assert f.exists()


@pytest.mark.unit
def test_assert_nonzero_write_zero_byte(tmp_path: Path) -> None:
    """0-byte file: assert_nonzero_write raises EmptyOutputError and deletes the file."""
    from fieldkit.util.atomic import assert_nonzero_write

    f = tmp_path / "output.md"
    f.write_text("", encoding="utf-8")
    with pytest.raises(EmptyOutputError, match="Empty output detected"):
        assert_nonzero_write(f)
    assert not f.exists()
