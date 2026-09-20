"""Branch coverage tests for fieldkit.ingest.reprocess._reprocess_transcript_ingest.

Covers: --from-version guard, dry-run paths, service auth error, interactive
mode (y/n/q), KeyboardInterrupt, _reprocess_one_artifact error handling,
and artifact write failure.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.ingest.db import ArtifactRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact(
    source_id: str = "src-001",
    artifact_id: str = "art-001",
    pipeline_version: str = "0.1.0",
    content_path: str = "/vault/note.md",
) -> ArtifactRecord:
    return ArtifactRecord(
        source_id=source_id,
        artifact_id=artifact_id,
        pipeline_id="transcript-ingest",
        pipeline_version=pipeline_version,
        content_path=content_path,
        created_at="2026-01-01T00:00:00",
    )


def _make_spec(version: str = "0.2.0") -> SimpleNamespace:
    return SimpleNamespace(status="active", version=version)


def _make_pipeline_map(spec: SimpleNamespace) -> dict[str, SimpleNamespace]:
    return {"transcript-ingest": spec}


# ---------------------------------------------------------------------------
# _run_reprocess — pipeline validation
# ---------------------------------------------------------------------------


# ── TestRunReprocess (flattened) ────────────────────────────────────────────


def test_run_reprocess_unknown_pipeline_returns_1() -> None:
    from fieldkit.commands.ingest.reprocess import _run_reprocess

    with patch("fieldkit.commands.ingest.registry.PIPELINE_MAP", {"transcript-ingest": _make_spec()}):
        rc = _run_reprocess(
            pipeline_id="nonexistent-pipeline",
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert rc == 1


def test_run_reprocess_stub_pipeline_returns_1() -> None:
    from fieldkit.commands.ingest.reprocess import _run_reprocess

    stub_spec = SimpleNamespace(status="stub", version="0.1.0")
    with patch("fieldkit.commands.ingest.registry.PIPELINE_MAP", {"transcript-ingest": stub_spec}):
        rc = _run_reprocess(
            pipeline_id="transcript-ingest",
            from_version=None,
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert rc == 1


def test_run_reprocess_future_pipeline_id_returns_1() -> None:
    from fieldkit.commands.ingest.reprocess import _run_reprocess

    future_spec = SimpleNamespace(status="active", version="0.2.0")
    with patch(
        "fieldkit.commands.ingest.registry.PIPELINE_MAP",
        {"transcript-ingest": future_spec, "future-pipeline": future_spec},
    ):
        rc = _run_reprocess(
            pipeline_id="future-pipeline",
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert rc == 1


def test_force_and_from_version_exit_3_before_runner(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.__main__ import main

    with patch("fieldkit.commands.ingest.reprocess._run_reprocess") as run_reprocess:
        exit_code = main(
            [
                "ingest",
                "reprocess",
                "--pipeline",
                "transcript-ingest",
                "--force",
                "--from-version",
                "0.1.0",
            ]
        )

    assert exit_code == 3
    assert "--force cannot be combined with --from-version" in capsys.readouterr().err
    run_reprocess.assert_not_called()


# ---------------------------------------------------------------------------
# _reprocess_transcript_ingest — from_version guard
# ---------------------------------------------------------------------------


# ── TestReprocessFromVersionGuard (flattened) ───────────────────────────────


def test_run_reprocess_no_from_version_live_run_returns_1(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()
    artifacts: list[Any] = []

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=artifacts),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version=None,
            dry_run=False,
            interactive=False,
            limit=None,
        )

    assert rc == 1


def test_run_reprocess_no_from_version_dry_run_with_artifacts_returns_1(tmp_path: Path) -> None:
    """dry_run + no from_version: displays artifacts but returns 1."""
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()
    art = _make_artifact()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[art]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version=None,
            dry_run=True,
            interactive=False,
            limit=None,
        )

    assert rc == 1


def test_run_reprocess_no_from_version_dry_run_empty_returns_1(tmp_path: Path) -> None:
    """dry_run + no from_version + empty artifacts: returns 1 (guard + no-op)."""
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version=None,
            dry_run=True,
            interactive=False,
            limit=None,
        )

    assert rc == 1


def test_run_reprocess_from_version_dry_run_no_artifacts_returns_0(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )

    assert rc == 0


def test_run_reprocess_from_version_dry_run_lists_artifacts(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec(version="0.2.0")
    art = _make_artifact(pipeline_version="0.1.0")

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[art]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=True,
            interactive=False,
            limit=None,
        )

    assert rc == 0


def test_run_reprocess_from_version_dry_run_with_limit(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec(version="0.2.0")
    arts = [_make_artifact(f"src-{i}", f"art-{i}") for i in range(3)]

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=arts),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=True,
            interactive=False,
            limit=2,
        )

    assert rc == 0


# ---------------------------------------------------------------------------
# _reprocess_transcript_ingest — service auth error
# ---------------------------------------------------------------------------


# ── TestReprocessServiceAuthError (flattened) ───────────────────────────────


def test_run_reprocess_docs_service_not_found_returns_1(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()
    art = _make_artifact()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[art]),
        patch("fieldkit.ingest.docs.get_docs_service", side_effect=FileNotFoundError("token missing")),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )

    assert rc == 1


# ---------------------------------------------------------------------------
# _reprocess_transcript_ingest — live run with mocked artifact processing
# ---------------------------------------------------------------------------


# ── TestReprocessLiveRun (flattened) ────────────────────────────────────────


def _run_reprocess_run_with_mock_process(
    artifacts: list[Any],
    *,
    process_ok: bool = True,
    interactive: bool = False,
    interactive_choices: list[str] | None = None,
) -> int:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec(version="0.2.0")
    choices = iter(interactive_choices or [])

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=artifacts),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch(
            "fieldkit.commands.ingest.reprocess._reprocess_one_artifact",
            return_value=process_ok,
        ),
        patch("fieldkit.commands.ingest.reprocess._prompt_reprocess_choice", side_effect=choices),
    ):
        return _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=False,
            interactive=interactive,
            limit=None,
        )


def test_run_reprocess_successful_artifact_returns_0() -> None:
    rc = _run_reprocess_run_with_mock_process([_make_artifact()])
    assert rc == 0


def test_run_reprocess_failed_artifact_preserves_human_exit() -> None:
    """Human mode preserves its historical zero exit after reporting errors."""
    rc = _run_reprocess_run_with_mock_process([_make_artifact()], process_ok=False)
    assert rc == 0


def test_run_reprocess_multiple_artifacts_all_succeed() -> None:
    arts = [_make_artifact(f"src-{i}", f"art-{i}") for i in range(3)]
    rc = _run_reprocess_run_with_mock_process(arts, process_ok=True)
    assert rc == 0


def test_run_reprocess_json_reports_ordered_partial_outcomes(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    artifacts = [_make_artifact(f"src-{i}", f"art-{i}") for i in range(3)]
    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=artifacts),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact", side_effect=[True, False, True]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=_make_spec(version="0.2.0"),
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
            as_json=True,
        )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["completed"] == ["src-0", "src-2"]
    assert payload["failed"] == ["src-1"]


def test_run_reprocess_json_interrupt_preserves_retry_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    artifacts = [_make_artifact(f"src-{i}", f"art-{i}") for i in range(3)]
    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=artifacts),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact", side_effect=[True, KeyboardInterrupt]),
    ):
        rc = _reprocess_transcript_ingest(
            spec=_make_spec(version="0.2.0"),
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
            as_json=True,
        )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["completed"] == ["src-0"]
    assert payload["pending"] == ["src-1", "src-2"]


def test_run_reprocess_interactive_yes_processes() -> None:
    rc = _run_reprocess_run_with_mock_process(
        [_make_artifact()],
        interactive=True,
        interactive_choices=["y"],
        process_ok=True,
    )
    assert rc == 0


def test_run_reprocess_interactive_no_skips() -> None:
    rc = _run_reprocess_run_with_mock_process(
        [_make_artifact()],
        interactive=True,
        interactive_choices=["n"],
    )
    assert rc == 0


def test_run_reprocess_interactive_quit_stops() -> None:
    arts = [_make_artifact("src-1", "art-1"), _make_artifact("src-2", "art-2")]
    rc = _run_reprocess_run_with_mock_process(
        arts,
        interactive=True,
        interactive_choices=["q"],
    )
    assert rc == 0


def test_run_reprocess_keyboard_interrupt_returns_0() -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    spec = _make_spec()
    art = _make_artifact()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.db.init_db", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[art]),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch(
            "fieldkit.commands.ingest.reprocess._reprocess_one_artifact",
            side_effect=KeyboardInterrupt,
        ),
    ):
        rc = _reprocess_transcript_ingest(
            spec=spec,
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )

    assert rc == 0


# ---------------------------------------------------------------------------
# _reprocess_one_artifact — write failure
# ---------------------------------------------------------------------------


# ── TestReprocessOneArtifact (flattened) ────────────────────────────────────


def test_reprocess_one_artifact_doc_fetch_returns_none_returns_false() -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    art = _make_artifact()

    with patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=None):
        result = _reprocess_one_artifact(art=art, service=MagicMock(), conn=MagicMock(), pipeline_version="0.2.0")

    assert result is False


def test_reprocess_one_artifact_write_oserror_returns_false(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    art = _make_artifact(content_path=str(tmp_path / "vault" / "note.md"))

    doc = SimpleNamespace(
        doc_title="Note",
        transcript_text="Alice: Hi.",
        notes_text=None,
        invited_emails=["alice@acme.example.com"],
    )

    with (
        patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=doc),
        patch(
            "fieldkit.ingest.router.route_by_domains",
            return_value=SimpleNamespace(accounts=["unknown"], pursuits=[]),
        ),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value="cleaned"),
        patch(
            "fieldkit.ingest.pipeline.stage2_extract",
            return_value=SimpleNamespace(
                confidence="high",
                accounts=["unknown"],
                pursuits=[],
            ),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="# Note"),
        patch.object(Path, "write_text", side_effect=OSError("disk full")),
    ):
        result = _reprocess_one_artifact(art=art, service=MagicMock(), conn=MagicMock(), pipeline_version="0.2.0")

    assert result is False


def test_reprocess_one_artifact_stage1_failure_graceful_degradation(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    vault = tmp_path / "vault.md"
    art = _make_artifact(content_path=str(vault))

    doc = SimpleNamespace(
        doc_title="Note",
        transcript_text="Raw text",
        notes_text=None,
        invited_emails=[],
    )

    meta = SimpleNamespace(
        confidence="low",
        accounts=["unknown"],
        pursuits=[],
    )

    with (
        patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=doc),
        patch(
            "fieldkit.ingest.router.route_by_domains",
            return_value=SimpleNamespace(accounts=["unknown"], pursuits=[]),
        ),
        patch("fieldkit.ingest.pipeline.stage1_clean", side_effect=RuntimeError("llm fail")),
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=meta),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="# Content"),
        patch("fieldkit.ingest.db.update_artifact_version"),
    ):
        result = _reprocess_one_artifact(art=art, service=MagicMock(), conn=MagicMock(), pipeline_version="0.2.0")

    # stage1 failure degrades gracefully — vault should still be written
    assert result is True
    assert vault.exists()


def test_reprocess_one_artifact_stage2_failure_graceful_degradation(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    vault = tmp_path / "vault.md"
    art = _make_artifact(content_path=str(vault))

    doc = SimpleNamespace(
        doc_title="Note",
        transcript_text="Raw text",
        notes_text=None,
        invited_emails=[],
    )

    with (
        patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=doc),
        patch(
            "fieldkit.ingest.router.route_by_domains",
            return_value=SimpleNamespace(accounts=["unknown"], pursuits=[]),
        ),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value="cleaned"),
        patch("fieldkit.ingest.pipeline.stage2_extract", side_effect=RuntimeError("extract fail")),
        patch(
            "fieldkit.ingest.pipeline.TranscriptMeta",
            return_value=SimpleNamespace(confidence="low", accounts=["unknown"], pursuits=[]),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="# Content"),
        patch("fieldkit.ingest.db.update_artifact_version"),
    ):
        result = _reprocess_one_artifact(art=art, service=MagicMock(), conn=MagicMock(), pipeline_version="0.2.0")

    assert result is True


# ---------------------------------------------------------------------------
# _fetch_doc_for_reprocess — error paths
# ---------------------------------------------------------------------------


# ── TestFetchDocForReprocess (flattened) ────────────────────────────────────


def test_fetch_doc_for_reprocess_not_found_returns_none() -> None:
    from fieldkit.commands.ingest.reprocess import _fetch_doc_for_reprocess
    from fieldkit.ingest.docs import DocNotFoundError

    with patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=DocNotFoundError("404")):
        result = _fetch_doc_for_reprocess(MagicMock(), "src-xyz")

    assert result is None


def test_fetch_doc_for_reprocess_access_denied_returns_none() -> None:
    from fieldkit.commands.ingest.reprocess import _fetch_doc_for_reprocess
    from fieldkit.ingest.docs import DocAccessDeniedError

    with patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=DocAccessDeniedError("403")):
        result = _fetch_doc_for_reprocess(MagicMock(), "src-xyz")

    assert result is None


def test_fetch_doc_for_reprocess_generic_error_returns_none() -> None:
    from fieldkit.commands.ingest.reprocess import _fetch_doc_for_reprocess

    with patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=RuntimeError("network error")):
        result = _fetch_doc_for_reprocess(MagicMock(), "src-xyz")

    assert result is None
