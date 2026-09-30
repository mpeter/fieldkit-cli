"""Versioned task provenance and canonical rendering for meeting writebacks."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.util.owned_markdown import OwnedMarker, read_owned_markers

_MARKER_PREFIX = "fieldkit-task:"
_SOURCE_ID = re.compile(r"[A-Za-z0-9_:-]{1,256}")


@dataclass(frozen=True)
class TaskEffect:
    """One decision at its original, unfiltered position in a stable source."""

    source_id: str
    position: int
    item: ClassifiedItem


@dataclass(frozen=True)
class PreparedTaskLine:
    """Exact rendered effect plus independently comparable ownership and meaning."""

    identity: str
    fingerprint: str
    line: str


def format_task(item: ClassifiedItem, *, meeting_date: str, meeting_title: str) -> str:
    """Render the shared active/waiting-on task convention without provenance."""
    tag = f"**[{item.pursuit_label}]** " if item.pursuit_label else ""
    if item.cls == ItemClass.MY_TASK:
        return f"- {tag}{item.text}"
    if item.cls != ItemClass.WAITING_ON:
        raise ValueError("Cannot render a dropped task")
    owner = f" {item.owner}" if item.owner not in ("Unknown", "Team") else ""
    topic = re.sub(r"^[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*\s*[:\—\-]\s*", "", item.text).strip()
    return f"- {tag}Waiting on{owner} re: {topic} — from {meeting_title} ({meeting_date})"


def _validate_rendered_text(value: str) -> None:
    """Reject multiline, control, and reserved provenance syntax before writing."""
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(unicodedata.category(character).startswith("C") or character in "\u2028\u2029" for character in value)
        or "<!--" in value
        or "-->" in value
        or _MARKER_PREFIX in value
    ):
        raise ValueError("Invalid task rendering input")


def validate_task_source_id(value: object) -> str:
    """Accept bounded exact identities, including the ambient source namespace."""
    if not isinstance(value, str) or not _SOURCE_ID.fullmatch(value):
        raise ValueError("Invalid task source identity")
    return value


def _digest(parts: list[str | int]) -> str:
    """Freeze v1 identities as SHA-256 of compact ASCII JSON arrays."""
    raw = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def validate_effect(effect: TaskEffect, *, meeting_date: str, meeting_title: str) -> None:
    """Check effect admission before journaling or rendering any output."""
    validate_task_source_id(effect.source_id)
    if type(effect.position) is not int or not 0 <= effect.position < 2000:
        raise ValueError("Invalid task position")
    item = effect.item
    for value in (item.text, item.owner, item.pursuit_label, meeting_date, meeting_title):
        _validate_rendered_text(value)
    if not item.text.strip():
        raise ValueError("Invalid task rendering input")
    if item.cls == ItemClass.WAITING_ON and not item.owner.strip():
        raise ValueError("Invalid task rendering input")
    if item.pursuit_label and not item.pursuit_label.strip():
        raise ValueError("Invalid task rendering input")


def prepare_effect(effect: TaskEffect, *, meeting_date: str, meeting_title: str) -> PreparedTaskLine:
    """Bind original position and saved meaning, never the user's later edits."""
    validate_effect(effect, meeting_date=meeting_date, meeting_title=meeting_title)
    item = effect.item
    identity = _digest([1, effect.source_id, effect.position])
    semantics: list[str | int] = [1, item.text, item.cls.value, item.pursuit_label]
    if item.cls == ItemClass.WAITING_ON:
        semantics.extend([item.owner, meeting_date, meeting_title])
    fingerprint = _digest(semantics)
    if item.cls == ItemClass.DROP:
        return PreparedTaskLine(identity, fingerprint, "")
    line = format_task(item, meeting_date=meeting_date, meeting_title=meeting_title)
    return PreparedTaskLine(identity, fingerprint, f"{line} <!-- {_MARKER_PREFIX}v1:{identity}:{fingerprint} -->")


def read_task_markers(content: str) -> dict[str, OwnedMarker]:
    """Reject malformed, orphaned, duplicate, and unknown-version task markers."""
    return read_owned_markers(
        content,
        prefix=_MARKER_PREFIX,
        section_titles=("Active", "Waiting On"),
        error_message="Invalid or ambiguous task provenance",
    )
