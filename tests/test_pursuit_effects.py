"""Pursuit activity ownership preserves edits and rejects ambiguous replay."""

import hashlib
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from fieldkit.ingest.paths import compute_vault_path
from fieldkit.ingest.prepared import PreparedMeeting, ReplayIntent
from fieldkit.ingest.pursuit_effect import insert_owned_activity, publish_prepared_pursuit

pytestmark = pytest.mark.unit

_IDENTITY = "a" * 64
_FINGERPRINT = "b" * 64


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PreparedMeeting:
    monkeypatch.setattr("fieldkit.pursuit.io.get_fieldkit_data", lambda: tmp_path / "runtime")
    return PreparedMeeting(
        schema_version=1,
        pipeline_id="transcript-ingest",
        source_id="source-1",
        pipeline_version="0.1.0",
        vault_relative_path=compute_vault_path(
            Path(), "acme", "2026-09-27", "Meeting", source_id="source-1"
        ).as_posix(),
        note_content="Meeting",
        note_sha256=hashlib.sha256(b"Meeting").hexdigest(),
        account="acme",
        meeting_date="2026-09-27",
        meeting_title="Meeting",
        pursuits=("project",),
        action_items=(),
        tasks=(),
        degraded=False,
    )


def test_prepared_pursuit_publication_preserves_edits(tmp_path: Path, prepared: PreparedMeeting) -> None:
    path = tmp_path / "accounts/acme/pursuits/project.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\nstage: "qualify"\n---\n\n## Activity Log\n', encoding="utf-8")
    assert publish_prepared_pursuit(ReplayIntent(prepared), tmp_path, "project") is True
    edited = path.read_text(encoding="utf-8").replace("Meeting note:", "Human revision:")
    path.write_text(edited, encoding="utf-8")
    assert publish_prepared_pursuit(ReplayIntent(prepared), tmp_path, "project") is False
    assert path.read_text(encoding="utf-8") == edited


def test_missing_prepared_pursuit_fails(tmp_path: Path, prepared: PreparedMeeting) -> None:
    with pytest.raises(FileNotFoundError, match="project"):
        publish_prepared_pursuit(ReplayIntent(prepared), tmp_path, "project")
    assert not (tmp_path / "accounts/acme/pursuits/project.md").exists()


@pytest.mark.parametrize("redirect", ["leaf", "ancestor"])
def test_prepared_pursuit_rejects_redirect(tmp_path: Path, prepared: PreparedMeeting, redirect: str) -> None:
    path = tmp_path / "accounts/acme/pursuits/project.md"
    outside = tmp_path / "other"
    outside.mkdir()
    target = outside / "project.md"
    target.write_text("untouched", encoding="utf-8")
    if redirect == "leaf":
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
    else:
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match=r"escapes workspace|redirects its authorized destination"):
        publish_prepared_pursuit(ReplayIntent(prepared), tmp_path, "project")
    assert target.read_text(encoding="utf-8") == "untouched"


def test_unprepared_pursuit_is_rejected(tmp_path: Path, prepared: PreparedMeeting) -> None:
    with pytest.raises(ValueError, match="not a prepared target"):
        publish_prepared_pursuit(ReplayIntent(prepared), tmp_path, "../other")
    assert list(tmp_path.iterdir()) == []


def test_punctuation_title_is_one_literal_link(tmp_path: Path, prepared: PreparedMeeting) -> None:
    title = r"[Proposal](https://example.com) *review* <tag> & `code` ! \\"
    changed = prepared.model_copy(
        update={
            "meeting_title": title,
            "vault_relative_path": compute_vault_path(
                Path(), "acme", "2026-09-27", title, source_id="source-1"
            ).as_posix(),
        }
    )
    path = tmp_path / "accounts/acme/pursuits/project.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nstage: qualify\n---\n\n## Activity Log\n", encoding="utf-8")
    assert publish_prepared_pursuit(ReplayIntent(changed), tmp_path, "project") is True
    tokens = MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8"))
    inline = [child for token in tokens if token.type == "inline" for child in (token.children or [])]
    links = [token for token in inline if token.type == "link_open"]
    assert len(links) == 1
    assert links[0].attrGet("href") == "../meetings/" + Path(changed.vault_relative_path).name
    assert any(token.type == "text" and token.content == title for token in inline)


def test_activity_retry_preserves_edited_entry() -> None:
    original = "\n## Activity Log\n"
    result = insert_owned_activity(original, "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT)
    assert result.startswith(original + "- Meeting <!-- fieldkit-ingest-pursuit:v1:")
    edited = result.replace("- Meeting", "- Human revision")
    assert insert_owned_activity(edited, "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT) == edited


@pytest.mark.parametrize("mutation", ["duplicate", "fenced", "conflict", "orphan", "wrong_section"])
def test_activity_rejects_ambiguous_marker(mutation: str) -> None:
    result = insert_owned_activity("\n## Activity Log\n", "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT)
    altered = {
        "duplicate": result + result.splitlines()[-1] + "\n",
        "fenced": "\n```\n" + result + "```\n",
        "conflict": result.replace(_FINGERPRINT, "c" * 64),
        "orphan": result.replace("- Meeting ", "- "),
        "wrong_section": result.replace("Activity Log", "Notes"),
    }[mutation]
    with pytest.raises(ValueError, match="Conflicting pursuit activity ownership"):
        insert_owned_activity(altered, "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT)


def test_activity_requires_legacy_reconciliation() -> None:
    with pytest.raises(ValueError, match="Unmarked pursuit activity requires reconciliation"):
        insert_owned_activity(
            "\n## Activity Log\n- Meeting\n", "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT
        )


def test_activity_cannot_be_inserted_inside_unclosed_fence() -> None:
    with pytest.raises(ValueError, match="Conflicting pursuit activity ownership"):
        insert_owned_activity("\n```\n", "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT)


def test_activity_heading_without_final_newline() -> None:
    result = insert_owned_activity("\n## Activity Log", "- Meeting", identity=_IDENTITY, fingerprint=_FINGERPRINT)
    assert result.startswith("\n## Activity Log\n- Meeting ")


@pytest.mark.parametrize("title", ["\nHeading", "private\x00title", "\u2028Heading", "   "])
def test_invalid_title_fails_before_filesystem_effects(tmp_path: Path, prepared: PreparedMeeting, title: str) -> None:
    changed = prepared.model_copy(
        update={
            "meeting_title": title,
            "vault_relative_path": compute_vault_path(
                Path(), "acme", "2026-09-27", title, source_id="source-1"
            ).as_posix(),
        }
    )
    with pytest.raises(ValueError, match="Invalid prepared ingest output"):
        publish_prepared_pursuit(ReplayIntent(changed), tmp_path, "project")
    assert list(tmp_path.iterdir()) == []
