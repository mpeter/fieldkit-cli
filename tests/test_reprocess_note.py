"""Reprocessing replaces only the unchanged, source-owned meeting note."""

import hashlib
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.ingest.db import ArtifactRecord, init_db
from fieldkit.ingest.reprocess_note import ReprocessNote, capture_reprocess_note
from fieldkit.ingest.reprocess_replay import prepare_reprocess, replay_reprocess
from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path

pytestmark = pytest.mark.unit


def _publish_replacement(
    artifact: ArtifactRecord, snapshot: ReprocessNote, content: str, *, runtime_root: Path
) -> Path:
    """Exercise note safety through the real journal and replay lifecycle."""
    with closing(init_db(runtime_root.parent / "reprocess-test.db")) as conn:
        conn.execute(
            "INSERT INTO pipelines (pipeline_id, description, version, source_format) VALUES (?, ?, ?, ?)",
            ("transcript-ingest", "Transcript", "0.1.0", "text"),
        )
        conn.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
            (artifact.source_id, "transcript-ingest", "source", "processed"),
        )
        conn.execute(
            "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.source_id,
                "transcript-ingest",
                "vault_note",
                artifact.content_path,
                "0.1.0",
            ),
        )
        conn.commit()
        intent = prepare_reprocess(
            conn,
            artifact=artifact,
            note=snapshot,
            content=content,
            to_version="0.2.0",
            runtime_root=runtime_root,
        )
        assert intent.artifact_id == artifact.artifact_id
        return replay_reprocess(conn, artifact.artifact_id, snapshot.workspace, runtime_root=runtime_root)


def test_reprocess_command_rejects_outside_path_before_fetch(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    art = _artifact(tmp_path / "outside")
    original = Path(art.content_path).read_bytes()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=workspace),
        patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=None) as fetch,
        patch("fieldkit.ingest.reprocess_replay.save_reprocess") as update,
    ):
        result = _reprocess_one_artifact(art=art, service=object(), conn=MagicMock(), pipeline_version="0.2.0")
    assert result is False
    fetch.assert_not_called()
    update.assert_not_called()
    assert Path(art.content_path).read_bytes() == original


@pytest.mark.parametrize(
    "key,value", [("source_id", "source-2"), ("pipeline", "different"), ("nested", "{a: 1, a: 2}")]
)
def test_reprocess_rejects_ambiguous_provenance_before_fetch(tmp_path: Path, key: str, value: str) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact

    art = _artifact(tmp_path)
    path = Path(art.content_path)
    original = _note().replace("---\n", f"---\n{key}: {value}\n", 1)
    path.write_text(original, encoding="utf-8")
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.ingest.reprocess._fetch_doc_for_reprocess", return_value=None) as fetch,
        patch("fieldkit.ingest.reprocess_replay.save_reprocess") as update,
    ):
        result = _reprocess_one_artifact(art=art, service=object(), conn=MagicMock(), pipeline_version="0.2.0")
    assert result is False
    fetch.assert_not_called()
    update.assert_not_called()
    assert path.read_text(encoding="utf-8") == original


def test_deep_frontmatter_is_a_fixed_validation_error() -> None:
    from fieldkit.ingest.writeback import parse_meeting_frontmatter

    content = "---\nkey: " + "[" * 1500 + "0" + "]" * 1500 + "\n---\nBody"
    with pytest.raises(ValueError, match=r"^Invalid meeting frontmatter$"):
        parse_meeting_frontmatter(content)


def test_reprocess_command_does_not_publish_ambiguous_generated_note(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact
    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.router import Confidence, RouteResult

    art = _artifact(tmp_path)
    path = Path(art.content_path)
    original = path.read_bytes()
    ambiguous = _note().replace("---\n", "---\nsource_id: different\n", 1)
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.ingest.docs.fetch_gemini_doc", return_value=GeminiDocContent("source-1", "Note", "Body", "Body")
        ),
        patch("fieldkit.ingest.router.route_by_domains", return_value=RouteResult(["acme"], Confidence.NONE, False)),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("Cleaned")),
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value=ambiguous),
        patch("fieldkit.ingest.reprocess_replay.atomic_text_write") as write,
        patch("fieldkit.ingest.reprocess_replay.save_reprocess") as update,
    ):
        result = _reprocess_one_artifact(art=art, service=object(), conn=MagicMock(), pipeline_version="0.2.0")
    assert result is False
    write.assert_not_called()
    update.assert_not_called()
    assert path.read_bytes() == original


def _note(body: str = "Old notes") -> str:
    return "---\nsource_id: source-1\npipeline: transcript-ingest\npipeline_version: 0.2.0\n---\n\n" + body + "\n"


def _artifact(workspace: Path, *, prepared: bool = False) -> ArtifactRecord:
    path = workspace / "accounts/acme/meetings/note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact_id = "prepared-v1:" + "a" * 64 if prepared else "legacy-1"
    content = _note()
    if prepared:
        source_digest = hashlib.sha256(b"fieldkit-ingest-note:v1:source-1").hexdigest()
        content += f"\n<!-- fieldkit-ingest-note:v1:{source_digest}:{'a' * 64} -->\n"
    path.write_text(content, encoding="utf-8")
    return ArtifactRecord(artifact_id, "source-1", "transcript-ingest", "0.1.0", str(path), "2026-09-27")


@pytest.mark.parametrize("prepared", [False, True])
def test_reprocess_preserves_ownership_and_permissions(tmp_path: Path, prepared: bool) -> None:
    art = _artifact(tmp_path, prepared=prepared)
    path = Path(art.content_path)
    path.chmod(0o640)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == path
    result = _publish_replacement(art, snapshot, _note("New notes"), runtime_root=tmp_path / "runtime")
    assert result == path
    assert _note("New notes") in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o640
    if prepared:
        assert path.read_text(encoding="utf-8").endswith(snapshot.marker + "\n")


@pytest.mark.parametrize(
    "mutation", ["outside", "leaf_link", "ancestor_link", "missing", "wrong_source", "unmarked", "duplicate", "fenced"]
)
def test_capture_rejects_unsafe_or_unowned_note(tmp_path: Path, mutation: str) -> None:
    workspace = tmp_path / "workspace"
    art = _artifact(workspace, prepared=True)
    path = Path(art.content_path)
    old = path.read_text(encoding="utf-8")
    if mutation == "outside":
        art.content_path = str(tmp_path / "outside.md")
    elif mutation == "leaf_link":
        target = tmp_path / "outside.md"
        target.write_text(old, encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
    elif mutation == "ancestor_link":
        moved = tmp_path / "moved"
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
    elif mutation == "missing":
        path.unlink()
    else:
        changed = {
            "wrong_source": old.replace("source_id: source-1", "source_id: source-2"),
            "unmarked": _note(),
            "duplicate": old + old,
            "fenced": "```markdown\n" + old + "```\n",
        }[mutation]
        path.write_text(changed, encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        capture_reprocess_note(art, workspace)


def test_reprocess_refuses_intervening_edit(tmp_path: Path) -> None:
    art = _artifact(tmp_path)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    snapshot.path.write_text(_note("User edit"), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        _publish_replacement(art, snapshot, _note("New notes"), runtime_root=tmp_path / "runtime")
    assert snapshot.path.read_text(encoding="utf-8") == _note("User edit")


@pytest.mark.parametrize("stored_alias", [False, True])
def test_reprocess_accepts_configured_workspace_alias(tmp_path: Path, stored_alias: bool) -> None:
    workspace = tmp_path / "workspace"
    art = _artifact(workspace)
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    if stored_alias:
        art.content_path = str(alias / "accounts/acme/meetings/note.md")
    snapshot = capture_reprocess_note(art, alias)
    assert snapshot.path == workspace / "accounts/acme/meetings/note.md"
    result = _publish_replacement(art, snapshot, _note("New notes"), runtime_root=tmp_path / "runtime")
    assert result == snapshot.path
    assert result.read_text(encoding="utf-8") == _note("New notes")


def test_reprocess_refuses_redirect_after_capture(tmp_path: Path) -> None:
    art = _artifact(tmp_path)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    moved = tmp_path / "moved"
    snapshot.path.parent.rename(moved)
    snapshot.path.parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="redirects"):
        _publish_replacement(art, snapshot, _note("New notes"), runtime_root=tmp_path / "runtime")
    assert (moved / "note.md").read_text(encoding="utf-8") == _note()


def test_reprocess_refuses_oversized_replacement(tmp_path: Path) -> None:
    art = _artifact(tmp_path)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    with (
        patch("fieldkit.ingest.note_effect.MAX_NOTE_BYTES", 128),
        pytest.raises(ValueError, match="bound"),
    ):
        _publish_replacement(art, snapshot, _note("é" * 100), runtime_root=tmp_path / "runtime")
    assert snapshot.path.read_text(encoding="utf-8") == _note()


def test_reprocess_uses_replay_note_lock(tmp_path: Path) -> None:
    art = _artifact(tmp_path)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    runtime = tmp_path / "runtime"
    lock = prepare_runtime_lock_path(snapshot.path, runtime, "meeting-note")
    with (
        exclusive_file_lock(lock),
        patch("fieldkit.ingest.note_effect.NOTE_WRITE_TIMEOUT_SECONDS", 0),
        pytest.raises(PathLockTimeoutError),
    ):
        _publish_replacement(art, snapshot, _note("New notes"), runtime_root=runtime)
    assert snapshot.path.read_text(encoding="utf-8") == _note()


def test_failed_replacement_preserves_original(tmp_path: Path) -> None:
    art = _artifact(tmp_path)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    with (
        patch("fieldkit.util.atomic.Path.replace", side_effect=OSError("synthetic failure")),
        pytest.raises(OSError, match="synthetic failure"),
    ):
        _publish_replacement(art, snapshot, _note("New notes"), runtime_root=tmp_path / "runtime")
    assert snapshot.path.read_text(encoding="utf-8") == _note()
    assert not list(snapshot.path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# No frontmatter",
        _note().replace("source-1", "source-2"),
        _note("```\nUnclosed fence"),
        _note().replace("---\n", "---\nsource_id: source-2\n", 1),
        _note().replace("---\n", "---\npipeline: different\n", 1),
        _note().replace("---\n", "---\nnested: {key: 1, key: 2}\n", 1),
    ],
)
def test_reprocess_rejects_invalid_replacement(tmp_path: Path, content: str) -> None:
    art = _artifact(tmp_path, prepared=True)
    snapshot = capture_reprocess_note(art, tmp_path)
    assert snapshot.path == Path(art.content_path)
    old = snapshot.path.read_bytes()
    with pytest.raises(ValueError):
        _publish_replacement(art, snapshot, content, runtime_root=tmp_path / "runtime")
    assert snapshot.path.read_bytes() == old
