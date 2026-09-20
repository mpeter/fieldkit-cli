"""Completed-meeting writebacks owned by the ingest domain."""

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fieldkit.errors import FieldkitError
from fieldkit.pursuit.io import extract_frontmatter_text, parse_frontmatter, write_frontmatter_raw

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeetingWriteback:
    """The completed meeting data needed for pursuit and task writebacks."""

    pursuits: tuple[str, ...]
    action_items: tuple[str, ...]
    account: str
    meeting_date: str
    meeting_title: str
    data_root: Path
    vault_path: Path


@dataclass(frozen=True)
class WritebackNotice:
    """A render-neutral message produced by a completed-meeting writeback."""

    message: str
    err: bool = False


def apply_meeting_writebacks(request: MeetingWriteback) -> tuple[WritebackNotice, ...]:
    """Apply the secondary writes for a completed meeting.

    Pursuit activity-log failures propagate because they would leave a linked pursuit
    incomplete. Task synchronization remains best-effort and returns a warning notice
    instead of interrupting the ingest pipeline.

    Callers that can run concurrently must serialize shared pursuit and TASKS.md
    mutations around this call.
    """
    if request.pursuits:
        _append_meeting_to_pursuits(
            pursuits=request.pursuits,
            account=request.account,
            meeting_date=request.meeting_date,
            meeting_title=request.meeting_title,
            vault_path=request.vault_path,
            data_root=request.data_root,
        )
    if not request.action_items:
        return ()
    return _sync_action_items_to_tasks(
        action_items=request.action_items,
        pursuits=request.pursuits,
        account=request.account,
        meeting_date=request.meeting_date,
        meeting_title=request.meeting_title,
        data_root=request.data_root,
        vault_path=request.vault_path,
    )


def _append_meeting_to_pursuits(
    *,
    pursuits: Sequence[str],
    account: str,
    meeting_date: str,
    meeting_title: str,
    vault_path: Path,
    data_root: Path,
) -> None:
    """Append a meeting reference to each linked pursuit's activity log."""
    for slug in pursuits:
        _append_meeting_to_pursuit(
            data_root / "accounts" / account / "pursuits" / f"{slug}.md",
            meeting_date,
            meeting_title,
            vault_path,
        )


def _append_meeting_to_pursuit(
    pursuit_path: Path,
    meeting_date: str,
    meeting_title: str,
    vault_path: Path,
) -> None:
    """Append one meeting reference when the pursuit exists and lacks it."""
    if not pursuit_path.exists():
        return
    with pursuit_path.open(encoding="utf-8") as source:
        content = source.read()
        expected_mtime = os.fstat(source.fileno()).st_mtime
    updated_content = _add_meeting_activity(content, meeting_date, meeting_title, vault_path)
    if updated_content is None:
        return
    updated = parse_frontmatter(updated_content)
    if updated is None:
        raise FieldkitError(f"Cannot append meeting to pursuit without valid frontmatter: {pursuit_path}")
    frontmatter, body = updated
    write_frontmatter_raw(pursuit_path, frontmatter, body, expected_mtime=expected_mtime)


def _add_meeting_activity(content: str, meeting_date: str, meeting_title: str, vault_path: Path) -> str | None:
    """Return pursuit content with one idempotent meeting activity entry."""
    if vault_path.name in content:
        return None
    link = f"[{meeting_title}](../meetings/{vault_path.name})"
    entry = f"- {meeting_date} — Meeting note: {link}"
    if "## Activity Log" in content:
        return content.replace("## Activity Log\n", f"## Activity Log\n{entry}\n", 1)
    return content.rstrip() + f"\n\n## Activity Log\n{entry}\n"


_DISPLAY_OVERRIDES: dict[str, str] = {
    "acme-corp": "Acme-Corp",  # pii-guard: ignore
    "rhoai": "RHOAI",
    "aep": "AEP",
    "eda": "EDA",
    "aap": "AAP",
    "ocp": "OCP",
    "eap": "EAP",
    "hcs": "HCS",
    "sow": "SOW",
    "mfa": "MFA",
    "ads": "ADS",
    "leapp": "LEAPP",
    "rhocp": "RHOCP",
}


def _display_slug(slug: str) -> str:
    """Convert a slug to a display name using known abbreviation overrides."""
    parts = slug.replace("-", " ").split()
    return " ".join(_DISPLAY_OVERRIDES.get(part.lower(), part.title()) for part in parts)


def _build_pursuit_label(account: str, pursuits: Sequence[str]) -> str:
    """Build the pursuit label for TASKS.md tagging."""
    account_display = _DISPLAY_OVERRIDES.get(account.lower(), account.replace("-", " ").title())
    return f"{account_display} / {_display_slug(pursuits[0])}" if pursuits else account_display


def _load_attendee_names(vault_path: Path, known_internal_names: list[str]) -> tuple[list[str], list[str]]:
    """Load internal-team and stakeholder names from vault frontmatter."""
    import yaml as _yaml

    internal_team_names: list[str] = list(known_internal_names)
    stakeholder_names: list[str] = []
    if not vault_path.exists():
        return internal_team_names, stakeholder_names

    fm_text = extract_frontmatter_text(vault_path.read_text(encoding="utf-8"))
    if not fm_text:
        return internal_team_names, stakeholder_names
    try:
        fm = _yaml.safe_load(fm_text) or {}
        vault_internal = [str(name) for name in (fm.get("attendees_internal") or [])]
        internal_team_names = list(dict.fromkeys(internal_team_names + vault_internal))
        vault_external = [
            re.sub(r"\s*\([^)]*\)\s*$", "", str(name)).strip() for name in (fm.get("attendees_external") or [])
        ]
        internal_lower = {name.lower() for name in internal_team_names}
        stakeholder_names = [
            name
            for name in vault_external
            if not any(internal in name.lower() or name.lower() in internal for internal in internal_lower)
        ]
    except Exception:  # noqa: BLE001
        logger.debug("ingest: failed to extract stakeholder names from vault frontmatter", exc_info=True)
    return internal_team_names, stakeholder_names


def _sync_action_items_to_tasks(
    *,
    action_items: Sequence[str],
    pursuits: Sequence[str],
    account: str,
    meeting_date: str,
    meeting_title: str,
    data_root: Path,
    vault_path: Path,
) -> tuple[WritebackNotice, ...]:
    """Classify action items and write MY_TASK items to TASKS.md."""
    try:
        from fieldkit.config import get_accounts_config, get_user_email, get_user_name
        from fieldkit.tasks.classifier import classify_action_items
        from fieldkit.tasks.writer import append_to_tasks

        user_name = get_user_name()
        user_email = get_user_email() or ""
        accounts = get_accounts_config()
        account_info = (accounts.get("accounts") or {}).get(account, {}) or {}
        known_internal_names: list[str] = list(account_info.get("internal_team_display_names") or [])
        internal_team_names, stakeholder_names = _load_attendee_names(vault_path, known_internal_names)
        classified = classify_action_items(
            list(action_items),
            user_name=user_name,
            user_email=user_email,
            stakeholder_names=stakeholder_names,
            internal_team_names=internal_team_names,
            pursuit_label=_build_pursuit_label(account, pursuits),
        )
        my_only = [item for item in classified if item.cls.value == "my_task"]
        if not my_only:
            return ()
        my_added, _ = append_to_tasks(
            my_only,
            data_root / "TASKS.md",
            meeting_date=meeting_date,
            meeting_title=meeting_title,
        )
        if not my_added:
            return ()
        return (
            WritebackNotice(
                f"  Tasks: +{my_added} active → TASKS.md  (run 'fieldkit ingest promote' to triage waiting-on items)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return (WritebackNotice(f"  Warning: action item sync failed: {exc}", err=True),)
