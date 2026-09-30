"""Source-owned pursuit activity effects for prepared ingest replay."""

import hashlib
import json
import re
import string
from pathlib import Path

from fieldkit.ingest.prepared import ReplayIntent, validate_pursuit_title
from fieldkit.pursuit.io import update_pursuit_body
from fieldkit.util.owned_markdown import inspect_owned_markdown, read_owned_markers
from fieldkit.util.workspace_paths import resolve_workspace_output

_PREFIX = "fieldkit-ingest-pursuit:"


def publish_prepared_pursuit(intent: ReplayIntent, workspace: Path, slug: str) -> bool:
    """Apply one required pursuit effect under the canonical pursuit lock.

    The workspace directory namespace must remain stable during publication.
    Existing symlink redirects are rejected; missing pursuits are not created.
    """
    prepared = intent.prepared
    if slug not in prepared.pursuits:
        raise ValueError("Pursuit is not a prepared target")
    title = prepared.meeting_title
    validate_pursuit_title(title)
    escaped_title = "".join("\\" + char if char in string.punctuation else char for char in title)
    filename = Path(prepared.vault_relative_path).name
    entry = f"- {prepared.meeting_date} — Meeting note: [{escaped_title}](../meetings/{filename})"
    identity = hashlib.sha256(
        json.dumps([_PREFIX, 1, prepared.source_id, prepared.account, slug], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fingerprint = intent.digest
    path = resolve_workspace_output(workspace, f"accounts/{prepared.account}/pursuits/{slug}.md")
    return update_pursuit_body(
        path, lambda body: insert_owned_activity(body, entry, identity=identity, fingerprint=fingerprint)
    )


def _activity_markers(content: str) -> dict[str, str]:
    """Only actual activity bullets may own one unique canonical marker."""
    markers = read_owned_markers(
        content,
        prefix=_PREFIX,
        section_titles=("Activity Log",),
        error_message="Conflicting pursuit activity ownership",
    )
    return {identity: marker.fingerprint for identity, marker in markers.items()}


def insert_owned_activity(content: str, entry: str, *, identity: str, fingerprint: str) -> str:
    """Preserve edited owned entries; refuse ambiguous or unmarked adoption."""
    if (
        re.fullmatch(r"[a-f0-9]{64}", identity) is None
        or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None
        or not entry.startswith("- ")
        or not entry[2:].strip()
        or len(entry.splitlines()) != 1
        or _PREFIX in entry
    ):
        raise ValueError("Invalid pursuit activity intent")
    markers = _activity_markers(content)
    if identity in markers:
        if markers[identity] != fingerprint:
            raise ValueError("Conflicting pursuit activity ownership")
        return content
    if entry in content.splitlines():
        raise ValueError("Unmarked pursuit activity requires reconciliation")
    marked = f"{entry} <!-- {_PREFIX}v1:{identity}:{fingerprint} -->\n"
    positions = inspect_owned_markdown(content, section_titles=("Activity Log",)).section_ends["Activity Log"]
    if positions:
        position = positions[0]
        prefix = content[:position]
        separator = "" if prefix.endswith("\n") else "\n"
        updated = prefix + separator + marked + content[position:]
    else:
        updated = content + "\n\n## Activity Log\n" + marked
    if _activity_markers(updated).get(identity) != fingerprint:
        raise ValueError("Conflicting pursuit activity ownership")
    return updated
