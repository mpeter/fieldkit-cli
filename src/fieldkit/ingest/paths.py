"""Canonical source-specific meeting note destinations."""

import hashlib
import re
import stat
from datetime import date
from pathlib import Path

from fieldkit.util import workspace_paths


def validate_meeting_account(account: str) -> str:
    """Require a literal bounded account slug, never a path or glob."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,255}", account) is None:
        raise ValueError("Invalid meeting account")
    return account


def resolve_promote_input(workspace: Path, path: Path) -> Path:
    """Reject redirects while allowing the configured workspace itself to be an alias.

    Explicit notes outside the workspace remain supported using their canonical
    path. Directory namespaces must remain stable during reads.
    """
    try:
        workspace_path = workspace.absolute()
        candidate = path.absolute()
        if candidate.is_relative_to(workspace_path):
            return workspace_paths.resolve_workspace_output(workspace, candidate.relative_to(workspace_path).as_posix())
        if candidate.resolve(strict=True) != candidate:
            raise ValueError("Meeting input redirects its selected path")
        return candidate
    except (OSError, RuntimeError):
        raise ValueError("Cannot resolve meeting input") from None


def recent_meeting_paths(workspace: Path, *, account: str | None, limit: int) -> list[Path]:
    """Select regular meeting files without traversing child directory redirects.

    Missing accounts/meetings directories are empty selections. Unreadable or
    unsafe discovered entries fail the entire selection before promotion starts.
    Stable directory namespaces are required, as with prepared note publication.
    """
    if account is not None:
        validate_meeting_account(account)
    try:
        root = workspace.resolve(strict=True)
        accounts = workspace_paths.resolve_workspace_output(root, "accounts")
        if not accounts.exists():
            return []
        account_dirs = [accounts / account] if account is not None else list(accounts.iterdir())
        candidates: list[tuple[int, Path]] = []
        for account_dir in account_dirs:
            if account is None:
                account_info = account_dir.stat(follow_symlinks=False)
                if stat.S_ISREG(account_info.st_mode):
                    continue
                if not stat.S_ISDIR(account_info.st_mode):
                    raise ValueError("Account entry is not a directory")
                if account_dir.name.startswith("."):
                    continue
            validate_meeting_account(account_dir.name)
            meetings = workspace_paths.resolve_workspace_output(root, f"accounts/{account_dir.name}/meetings")
            if not meetings.exists():
                continue
            for path in meetings.iterdir():
                if path.name.startswith(".") or path.suffix != ".md":
                    continue
                candidate = workspace_paths.resolve_workspace_output(root, path.relative_to(root).as_posix())
                info = candidate.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Meeting candidate is not a regular file")
                candidates.append((info.st_mtime_ns, candidate))
        candidates.sort(key=lambda entry: (-entry[0], entry[1].as_posix()))
        return [path for _, path in candidates[:limit]]
    except (OSError, RuntimeError):
        raise ValueError("Cannot safely discover meeting files") from None


def validate_meeting_relative_path(relative_path: str) -> str:
    """Restrict stored meeting artifacts to their documented workspace subtree."""
    workspace_paths.validate_relative_output_path(relative_path)
    if re.fullmatch(r"accounts/[A-Za-z0-9_-]+/meetings/[^/.][^/]*\.md", relative_path) is None:
        raise ValueError("Invalid reprocess meeting destination")
    return relative_path


def _slugify(text: str) -> str:
    """Lowercase text and collapse whitespace into filename-safe separators."""
    text = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    return re.sub(r"[\s-]+", "-", text).strip("-")


def compute_vault_path(
    data_root: Path,
    account: str,
    meeting_date: str,
    meeting_title: str,
    *,
    source_id: str,
) -> Path:
    """Allocate a bounded date/title/source-digest path without filesystem effects.

    The source digest distinguishes same-day, same-title documents. Existing
    artifacts retain their recorded paths; allocation never moves old notes.

    This naming algorithm is frozen for prepared-output schema v1. A naming
    change requires a new schema decoder while outstanding v1 records continue
    to use this algorithm. Golden-record tests protect that durable contract.
    """
    validate_meeting_account(account)
    try:
        canonical_date = date.fromisoformat(meeting_date).isoformat()
    except ValueError:
        raise ValueError("Invalid meeting date") from None
    if canonical_date != meeting_date:
        raise ValueError("Invalid meeting date")
    if not source_id or len(source_id) > 1024:
        raise ValueError("Invalid meeting source identity")
    if len(meeting_title) > 8192:
        raise ValueError("Meeting title exceeds its bound")
    slug = _slugify(meeting_title)[:100].rstrip("-") or "meeting"
    try:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise ValueError("Invalid meeting source identity") from None
    return data_root / "accounts" / account / "meetings" / f"{meeting_date}-{slug}-{digest}.md"
