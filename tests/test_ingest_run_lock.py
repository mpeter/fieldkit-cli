"""Transcript runs refuse live owners before discovery or database mutation."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest
from fieldkit.commands.ingest.run import _run_transcript_ingest
from fieldkit.util.atomic import PathLockTimeoutError, exclusive_path_lock

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("fails", [False, True])
def test_reprocess_closes_database_before_releasing_lock(tmp_path: Path, fails: bool) -> None:
    db_path = tmp_path / "pipeline.db"
    target = tmp_path / "pipeline.db.transcript-run"
    conn = MagicMock()

    def close() -> None:
        with pytest.raises(PathLockTimeoutError), exclusive_path_lock(target, timeout_seconds=0):
            pytest.fail("Reprocess released its run lock before closing the database")

    conn.close.side_effect = close
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.init_db", return_value=conn),
        patch("fieldkit.ingest.db.get_artifacts_for_reprocess", return_value=[]) as select,
        patch("fieldkit.ingest.reprocess_replay.preflight_reprocess", return_value=()),
    ):
        if fails:
            select.side_effect = PathLockTimeoutError("note publication timeout")
            with pytest.raises(PathLockTimeoutError, match="note publication timeout"):
                _reprocess_transcript_ingest(
                    spec=SimpleNamespace(version="1.0.0"),
                    from_version="0.1.0",
                    dry_run=False,
                    limit=None,
                    interactive=False,
                )
        else:
            result = _reprocess_transcript_ingest(
                spec=SimpleNamespace(version="1.0.0"),
                from_version="0.1.0",
                dry_run=False,
                limit=None,
                interactive=False,
            )
            assert result == 0
    conn.close.assert_called_once_with()
    with exclusive_path_lock(target, timeout_seconds=0):
        assert not db_path.exists()


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_busy_reprocess_has_no_database_or_provider_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], alias: bool, as_json: bool
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    configured = root / "pipeline.db"
    if alias:
        link = tmp_path / "alias"
        link.symlink_to(root, target_is_directory=True)
        configured = link / "pipeline.db"
    with (
        exclusive_path_lock(root / "pipeline.db.transcript-run"),
        patch("fieldkit.ingest.db.get_db_path", return_value=configured),
        patch("fieldkit.ingest.db.init_db") as initialize,
        patch("fieldkit.ingest.docs.get_docs_service") as service,
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="1.0.0"),
            from_version="0.1.0",
            dry_run=False,
            limit=None,
            interactive=False,
            as_json=as_json,
        )
    assert result == 1
    initialize.assert_not_called()
    service.assert_not_called()
    assert not configured.exists()
    output = capsys.readouterr()
    assert "already running" in output.err
    assert str(tmp_path) not in output.err
    if as_json:
        assert json.loads(output.out)["error"] == "ingest_busy"
    else:
        assert output.out == ""


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_busy_run_has_no_database_or_discovery_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], alias: bool, as_json: bool
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "pipeline.db"
    configured = db_path
    if alias:
        link = tmp_path / "alias"
        link.symlink_to(root, target_is_directory=True)
        configured = link / "pipeline.db"
    with (
        exclusive_path_lock(root / "pipeline.db.transcript-run"),
        patch("fieldkit.ingest.db.get_db_path", return_value=configured),
        patch("fieldkit.ingest.db.init_db") as initialize,
        patch("fieldkit.gmail.discover.scan_gemini_candidates") as discover,
    ):
        result = _run_transcript_ingest(
            spec=SimpleNamespace(version="1.0.0"), dry_run=False, limit=None, interactive=False, as_json=as_json
        )
    assert result == 1
    initialize.assert_not_called()
    discover.assert_not_called()
    assert not db_path.exists()
    output = capsys.readouterr()
    assert "already running" in output.err
    assert str(tmp_path) not in output.err
    if as_json:
        assert json.loads(output.out)["error"] == "ingest_busy"
    else:
        assert output.out == ""


def test_run_holds_lock_until_work_returns(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    target = tmp_path / "pipeline.db.transcript-run"

    def work(**kwargs: object) -> int:
        assert kwargs["db_path"] == db_path
        with pytest.raises(PathLockTimeoutError), exclusive_path_lock(target, timeout_seconds=0):
            pytest.fail("Run lock was released early")
        return 0

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.commands.ingest.run._run_locked_transcript_ingest", side_effect=work),
    ):
        result = _run_transcript_ingest(
            spec=SimpleNamespace(version="1.0.0"), dry_run=False, limit=None, interactive=False
        )
    assert result == 0
    with exclusive_path_lock(target, timeout_seconds=0):
        assert not db_path.exists()


def test_run_releases_lock_and_does_not_mislabel_body_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch(
            "fieldkit.commands.ingest.run._run_locked_transcript_ingest",
            side_effect=PathLockTimeoutError("task write timeout"),
        ),
        pytest.raises(PathLockTimeoutError, match="task write timeout"),
    ):
        _run_transcript_ingest(spec=SimpleNamespace(version="1.0.0"), dry_run=False, limit=None, interactive=False)
    with exclusive_path_lock(tmp_path / "pipeline.db.transcript-run", timeout_seconds=0):
        assert not db_path.exists()
