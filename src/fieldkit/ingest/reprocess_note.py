"""Confined, ownership-preserving replacement of existing meeting notes."""

from dataclasses import dataclass, field
from pathlib import Path

from fieldkit.ingest import note_effect
from fieldkit.ingest.db import ArtifactRow
from fieldkit.ingest.paths import validate_meeting_relative_path
from fieldkit.ingest.prepared import NOTE_MARKER_PREFIX, owns_prepared_note, prepared_note_marker
from fieldkit.ingest.writeback import parse_meeting_frontmatter
from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot
from fieldkit.util.workspace_paths import resolve_workspace_output


@dataclass(frozen=True)
class ReprocessNote:
    """Existing note identity captured before provider or model work begins."""

    workspace: Path
    relative_path: str
    source_id: str
    marker: str
    original: TextSnapshot = field(repr=False)

    @property
    def path(self) -> Path:
        """Canonical lexical destination; callers revalidate before I/O."""
        return self.workspace / self.relative_path


def _validate_source(content: str, source_id: str) -> None:
    """Require meeting provenance rather than trusting a database path alone."""
    frontmatter = parse_meeting_frontmatter(content)
    if frontmatter.get("source_id") != source_id or frontmatter.get("pipeline") != "transcript-ingest":
        raise ValueError("Conflicting reprocess note source")


def validate_reprocess_content(content: str, source_id: str, artifact_id: str) -> str:
    """Validate exact final note bytes and return the required origin marker."""
    if len(content) > note_effect.MAX_NOTE_BYTES or len(content.encode("utf-8")) > note_effect.MAX_NOTE_BYTES:
        raise ValueError("Reprocessed note exceeds its bound")
    _validate_source(content, source_id)
    marker = ""
    if artifact_id.startswith("prepared-v1:"):
        marker = prepared_note_marker(source_id, artifact_id.removeprefix("prepared-v1:"))
        if not owns_prepared_note(content, marker):
            raise ValueError("Conflicting prepared note ownership")
    elif NOTE_MARKER_PREFIX in content:
        raise ValueError("Conflicting legacy note ownership")
    return marker


def capture_reprocess_note(artifact: ArtifactRow, workspace: Path) -> ReprocessNote:
    """Authorize an existing meeting path and capture its stable bounded bytes.

    Configured workspace aliases are supported, but descendant redirects,
    missing notes, stale roots, and ambiguous ownership are not adopted.
    """
    root = workspace.resolve(strict=True)
    stored = Path(artifact.content_path)
    if stored.is_relative_to(workspace.absolute()):
        relative = stored.relative_to(workspace.absolute()).as_posix()
    elif stored.is_relative_to(root):
        relative = stored.relative_to(root).as_posix()
    else:
        raise ValueError("Reprocess note is outside the configured workspace")
    validate_meeting_relative_path(relative)
    path = resolve_workspace_output(root, relative)
    original = read_text_snapshot(path, max_bytes=note_effect.MAX_NOTE_BYTES)
    marker = validate_reprocess_content(original.content, artifact.source_id, artifact.artifact_id)
    return ReprocessNote(root, relative, artifact.source_id, marker, original)


def render_reprocessed_content(note: ReprocessNote, content: str) -> str:
    """Validate generated content and append the preserved ownership footer."""
    if len(content) > note_effect.MAX_NOTE_BYTES or len(content.encode("utf-8")) > note_effect.MAX_NOTE_BYTES:
        raise ValueError("Reprocessed note exceeds its bound")
    _validate_source(content, note.source_id)
    if NOTE_MARKER_PREFIX in content:
        raise ValueError("Reprocessed note uses reserved ownership namespace")
    if note.marker:
        content += "\n\n" + note.marker + "\n"
        if not owns_prepared_note(content, note.marker):
            raise ValueError("Invalid reprocessed note ownership")
    if len(content.encode("utf-8")) > note_effect.MAX_NOTE_BYTES:
        raise ValueError("Reprocessed note exceeds its bound")
    return content
