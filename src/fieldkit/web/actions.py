"""Authenticated dashboard actions routed through the companion gate."""

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fieldkit.companion.decide import ProposedAction
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.lifecycle import record_outcome
from fieldkit.companion.outbox import ApprovalStatus, claim_proposal, list_proposals, write_proposal
from fieldkit.companion.runner import ActionResult, run_action

ActionState = Literal["disabled", "proposed", "executed", "denied", "failed", "conflict"]
ActionRunner = Callable[..., ActionResult]
_ACCOUNT = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class WebActionOutcome:
    """Stable result returned by dashboard mutation endpoints."""

    state: ActionState
    message: str
    item_id: str
    proposal_name: str | None = None
    exit_code: int | None = None

    @property
    def approval_status(self) -> ApprovalStatus | None:
        """Return the typed claim status without changing the JSON record."""
        return None


@dataclass(frozen=True)
class _MissingProposalOutcome(WebActionOutcome):
    @property
    def approval_status(self) -> ApprovalStatus:
        return "missing"


@dataclass(frozen=True)
class _NotApprovableProposalOutcome(WebActionOutcome):
    @property
    def approval_status(self) -> ApprovalStatus:
        return "not-approvable"


def _item_id(payload: dict[str, str | None]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _result(result: ActionResult, item_id: str) -> WebActionOutcome:
    if result.denied:
        return WebActionOutcome("denied", "Action denied by the companion gate.", item_id, exit_code=result.exit_code)
    if result.exit_code == 0:
        return WebActionOutcome("executed", "Action completed.", item_id, exit_code=0)
    diagnostic = (result.stderr.strip() or result.stdout.strip() or "Action failed.")[-500:]
    return WebActionOutcome("failed", diagnostic, item_id, exit_code=result.exit_code)


def _dispatch(
    item: AttentionItem,
    argv: list[str],
    *,
    tier: str,
    allowlist: list[str],
    data_path: Path,
    runner: ActionRunner,
) -> WebActionOutcome:
    if tier not in {"propose", "act"}:
        return WebActionOutcome("disabled", "Writes require companion tier propose or act.", item.item_id)
    if tier == "propose":
        existing = next((entry for entry in list_proposals(data_path) if entry.item_id == item.item_id), None)
        if existing is not None:
            return WebActionOutcome("proposed", "Proposal already exists.", item.item_id, existing.name)
        action = ProposedAction("task-sync", item.summary, None, tuple(argv))
        target = write_proposal(data_path, item, action)
        return WebActionOutcome("proposed", "Proposal created for review.", item.item_id, target.name)
    return _result(
        runner(argv, tier="act", allowlist=allowlist, data_path=data_path, item_id=item.item_id), item.item_id
    )


def create_task(
    title: str,
    section: Literal["today", "active"],
    account: str | None,
    due: str | None,
    *,
    tier: str,
    allowlist: list[str],
    data_path: Path,
    runner: ActionRunner = run_action,
    now: dt.datetime | None = None,
) -> WebActionOutcome:
    """Create, propose, or disable one normalized Google Task mutation."""
    if tier not in {"propose", "act"}:
        return WebActionOutcome("disabled", "Writes require companion tier propose or act.", "")
    title = title.strip()
    account = account.strip() if account is not None else None
    due = due.strip() if due is not None else None
    if not title:
        raise ValueError("title must not be empty")
    if section not in {"today", "active"}:
        raise ValueError("section must be today or active")
    if account is not None and _ACCOUNT.fullmatch(account) is None:
        raise ValueError("account must contain only letters, numbers, underscores, or hyphens")
    if due is not None:
        try:
            if dt.date.fromisoformat(due).isoformat() != due:
                raise ValueError
        except ValueError as exc:
            raise ValueError("due must be a valid date in YYYY-MM-DD format") from exc
    payload = {"account": account, "due": due, "kind": "create", "section": section, "title": title}
    item_id = _item_id(payload)
    argv = ["gtask", "create", title, "--section", section]
    if account is not None:
        argv.extend(["--account", account])
    if due is not None:
        argv.extend(["--due", due])
    argv.append("--confirm")
    observed = (now or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC).date().isoformat()
    item = AttentionItem(
        item_id,
        "web/google-tasks-create",
        account,
        "info",
        f"Create Google Task: {title}",
        "web://tasks",
        "task-sync",
        observed,
    )
    return _dispatch(item, argv, tier=tier, allowlist=allowlist, data_path=data_path, runner=runner)


def complete_task(
    task_id: str,
    *,
    tier: str,
    allowlist: list[str],
    data_path: Path,
    runner: ActionRunner = run_action,
    now: dt.datetime | None = None,
) -> WebActionOutcome:
    """Complete, propose, or disable one Google Task mutation."""
    if tier not in {"propose", "act"}:
        return WebActionOutcome("disabled", "Writes require companion tier propose or act.", "")
    task_id = task_id.strip()
    if not task_id or any(ord(char) < 32 or ord(char) == 127 for char in task_id):
        raise ValueError("task id must be non-empty and contain no control characters")
    item_id = _item_id({"kind": "complete", "task_id": task_id})
    observed = (now or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC).date().isoformat()
    item = AttentionItem(
        item_id,
        "web/google-tasks-complete",
        None,
        "info",
        f"Complete Google Task: {task_id}",
        "web://tasks",
        "task-sync",
        observed,
    )
    return _dispatch(
        item,
        ["gtask", "complete", task_id, "--confirm"],
        tier=tier,
        allowlist=allowlist,
        data_path=data_path,
        runner=runner,
    )


def approve(
    name: str,
    *,
    tier: str,
    allowlist: list[str],
    data_path: Path,
    runner: ActionRunner = run_action,
) -> WebActionOutcome:
    """Claim and execute one recorded proposal at most once."""
    if tier not in {"propose", "act"}:
        return WebActionOutcome("disabled", "Writes require companion tier propose or act.", "", name)
    claim = claim_proposal(data_path, name)
    proposal = claim.proposal
    if claim.status != "claimed" or proposal is None or proposal.command_argv is None:
        outcome_type = _MissingProposalOutcome if claim.status == "missing" else _NotApprovableProposalOutcome
        return outcome_type("conflict", claim.status, (proposal.item_id or "") if proposal is not None else "", name)
    command_argv = proposal.command_argv
    try:
        result = runner(
            list(command_argv), tier="act", allowlist=allowlist, data_path=data_path, item_id=proposal.item_id
        )
        outcome = _result(result, proposal.item_id or "")
        finished = claim.finish(success=outcome.state == "executed")
        if not finished:
            return WebActionOutcome(
                "conflict", "Proposal changed while finalizing execution.", proposal.item_id or "", name
            )
        if outcome.state == "executed":
            record_outcome(
                data_path,
                proposal_name=name,
                item_id=proposal.item_id or "",
                outcome="approved",
            )
        return WebActionOutcome(outcome.state, outcome.message, proposal.item_id or "", name, outcome.exit_code)
    finally:
        claim.close()
