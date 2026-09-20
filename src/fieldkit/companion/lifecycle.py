"""Retention and terminal-outcome evidence for companion proposals."""

import datetime as dt
import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fieldkit.companion.decide import propose_for
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.outbox import (
    OutboxProposal,
    list_proposals,
    lock_proposal_for_retention,
    replace_invalid_command_proposal,
)
from fieldkit.companion.suppress import add_cooldown

DEFAULT_RETENTION = dt.timedelta(days=14)
DEFAULT_PRUNE_LIMIT = 25
MAX_PRUNE_LIMIT = 100
_OUTCOME_STEM = "companion-proposal-outcomes"
_OUTCOME_SUFFIX = ".jsonl"
_OUTCOME_NAME = re.compile(r"companion-proposal-outcomes-\d{4}-\d{2}\.jsonl")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

ProposalOutcome = Literal["approved", "expired"]
PruneSkipReason = Literal["unsafe", "conflict"]
ReconcileSkipReason = Literal["stale", "no-baseline", "conflict"]


@dataclass(frozen=True)
class ProposalOutcomeRecord:
    """One terminal proposal outcome retained after its outbox file is gone."""

    proposal_name: str
    item_id: str
    outcome: ProposalOutcome
    timestamp: str


@dataclass(frozen=True)
class PruneSkip:
    """A proposal deliberately preserved during a prune pass."""

    name: str
    reason: PruneSkipReason

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class PruneResult:
    """Stable preview or mutation result for one bounded prune pass."""

    confirmed: bool
    pending: int
    eligible: int
    selected: tuple[str, ...]
    retired: tuple[str, ...]
    skipped: tuple[PruneSkip, ...]
    remaining_eligible: int
    ignored_rate: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "confirmed": self.confirmed,
            "pending": self.pending,
            "eligible": self.eligible,
            "selected": list(self.selected),
            "retired": list(self.retired),
            "skipped": [skip.to_dict() for skip in self.skipped],
            "remaining_eligible": self.remaining_eligible,
            "ignored_rate": self.ignored_rate,
        }


@dataclass(frozen=True)
class ReconcileSkip:
    """One legacy proposal intentionally retained during reconciliation."""

    name: str
    reason: ReconcileSkipReason

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class ReconcileResult:
    """The bounded selection, replacement, and retention outcome."""

    confirmed: bool
    candidates: int
    selected: tuple[str, ...]
    reconciled: tuple[str, ...]
    skipped: tuple[ReconcileSkip, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "confirmed": self.confirmed,
            "candidates": self.candidates,
            "selected": list(self.selected),
            "reconciled": list(self.reconciled),
            "skipped": _reconcile_skips_to_dict(self.skipped),
        }


def _reconcile_skips_to_dict(skips: tuple[ReconcileSkip, ...]) -> list[dict[str, str]]:
    """Serialize retained reconciliation records for the CLI result contract."""
    return [skip.to_dict() for skip in skips]


def _moment(value: dt.datetime | None) -> dt.datetime:
    moment = value if value is not None else dt.datetime.now(tz=dt.UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(dt.UTC)


def _created_at(proposal: OutboxProposal) -> dt.datetime | None:
    if proposal.created_at is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(proposal.created_at)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.UTC)


def outcome_path(data_path: Path, *, when: dt.datetime | None = None) -> Path:
    """Return the month-rotated proposal-outcome journal path."""
    moment = _moment(when)
    return data_path / f"{_OUTCOME_STEM}-{moment.strftime('%Y-%m')}{_OUTCOME_SUFFIX}"


def record_outcome(
    data_path: Path,
    *,
    proposal_name: str,
    item_id: str,
    outcome: ProposalOutcome,
    when: dt.datetime | None = None,
) -> Path:
    """Append one private lifecycle record with a single atomic write."""
    if Path(proposal_name).name != proposal_name or not proposal_name:
        raise ValueError("proposal_name must be a basename")
    moment = _moment(when)
    path = outcome_path(data_path, when=moment)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_fd = os.open(data_path, _DIRECTORY_FLAGS)
    file_fd = -1
    try:
        file_fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("proposal outcome journal must be an operator-owned regular file")
        os.fchmod(file_fd, 0o600)
        payload = {
            "proposal_name": proposal_name,
            "item_id": item_id,
            "outcome": outcome,
            "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        body = memoryview((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
        fcntl.flock(file_fd, fcntl.LOCK_EX)
        original_size = os.fstat(file_fd).st_size
        try:
            try:
                while body:
                    written = os.write(file_fd, body)
                    if written <= 0:
                        raise OSError("proposal outcome journal write was incomplete")
                    body = body[written:]
                os.fsync(file_fd)
            except BaseException as error:
                try:
                    os.ftruncate(file_fd, original_size)
                    os.fsync(file_fd)
                except OSError as rollback_error:
                    error.add_note(f"proposal outcome journal rollback also failed: {rollback_error}")
                raise
        finally:
            fcntl.flock(file_fd, fcntl.LOCK_UN)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)
    return path


def _parse_outcome(value: Any) -> ProposalOutcomeRecord | None:
    if not isinstance(value, dict):
        return None
    proposal_name = value.get("proposal_name")
    item_id = value.get("item_id")
    outcome = value.get("outcome")
    timestamp = value.get("timestamp")
    if (
        not isinstance(proposal_name, str)
        or Path(proposal_name).name != proposal_name
        or not isinstance(item_id, str)
        or outcome not in {"approved", "expired"}
        or not isinstance(timestamp, str)
    ):
        return None
    return ProposalOutcomeRecord(proposal_name, item_id, outcome, timestamp)


def read_outcomes(data_path: Path) -> list[ProposalOutcomeRecord]:
    """Read valid lifecycle records, ignoring corrupt lines but not unsafe files."""
    records: list[ProposalOutcomeRecord] = []
    try:
        directory_fd = os.open(data_path, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return records
    try:
        names = sorted(
            name
            for name in os.listdir(directory_fd)  # noqa: PTH208  # descriptor-relative containment
            if _OUTCOME_NAME.fullmatch(name)
        )
        for name in names:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise OSError("proposal outcome journal must be an operator-owned private regular file")
                with os.fdopen(file_fd, encoding="utf-8") as handle:
                    file_fd = -1
                    for line in handle:
                        try:
                            record = _parse_outcome(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                        if record is not None:
                            records.append(record)
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
    finally:
        os.close(directory_fd)
    return records


def ignored_rate(data_path: Path) -> float | None:
    """Return unique expired / terminal proposal outcomes, or None without evidence."""
    latest: dict[str, ProposalOutcome] = {}
    for record in read_outcomes(data_path):
        latest[record.proposal_name] = record.outcome
    if not latest:
        return None
    return sum(outcome == "expired" for outcome in latest.values()) / len(latest)


def _partition_proposals(
    proposals: list[OutboxProposal], *, moment: dt.datetime, older_than: dt.timedelta
) -> tuple[list[OutboxProposal], list[PruneSkip]]:
    eligible: list[tuple[dt.datetime, OutboxProposal]] = []
    unsafe: list[PruneSkip] = []
    for proposal in proposals:
        created = _created_at(proposal)
        if not proposal.approvable or created is None or proposal.item_id is None:
            unsafe.append(PruneSkip(proposal.name, "unsafe"))
        elif moment - created >= older_than:
            eligible.append((created, proposal))
    eligible.sort(key=lambda row: (row[0], row[1].name))
    return [proposal for _, proposal in eligible], unsafe


def _retire_candidate(data_path: Path, candidate: OutboxProposal, *, moment: dt.datetime) -> bool:
    retention = lock_proposal_for_retention(data_path, candidate.name)
    try:
        current = retention.proposal
        if retention.status != "locked" or current is None or current != candidate:
            return False
        add_cooldown(data_path, current.item_id or "", reason="proposal-expired", now=moment)
        if not retention.finish():
            return False
        record_outcome(
            data_path,
            proposal_name=candidate.name,
            item_id=current.item_id or "",
            outcome="expired",
            when=moment,
        )
        return True
    finally:
        retention.close()


def prune_proposals(
    data_path: Path,
    *,
    older_than: dt.timedelta = DEFAULT_RETENTION,
    limit: int = DEFAULT_PRUNE_LIMIT,
    confirm: bool = False,
    now: dt.datetime | None = None,
) -> PruneResult:
    """Preview or retire a bounded oldest-first set of pending proposals."""
    if older_than <= dt.timedelta(0):
        raise ValueError("older_than must be positive")
    if not 1 <= limit <= MAX_PRUNE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRUNE_LIMIT}")
    moment = _moment(now)
    proposals = list_proposals(data_path)
    eligible, unsafe = _partition_proposals(proposals, moment=moment, older_than=older_than)
    chosen = tuple(eligible[:limit])
    selected = tuple(proposal.name for proposal in chosen)
    if not confirm:
        return PruneResult(
            False, len(proposals), len(eligible), selected, (), tuple(unsafe), len(eligible), ignored_rate(data_path)
        )

    retired: list[str] = []
    skipped = list(unsafe)
    for candidate in chosen:
        if _retire_candidate(data_path, candidate, moment=moment):
            retired.append(candidate.name)
        else:
            skipped.append(PruneSkip(candidate.name, "conflict"))
    return PruneResult(
        True,
        len(proposals),
        len(eligible),
        selected,
        tuple(retired),
        tuple(skipped),
        max(0, len(eligible) - len(retired)),
        ignored_rate(data_path),
    )


def reconcile_invalid_command_proposals(
    data_path: Path, items: list[AttentionItem], *, limit: int = DEFAULT_PRUNE_LIMIT, confirm: bool = False
) -> ReconcileResult:
    """Rebuild fresh deterministic proposals for bounded legacy invalid-command records."""
    if not 1 <= limit <= MAX_PRUNE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRUNE_LIMIT}")
    selected, skipped, candidates = _select_reconciliation_candidates(data_path, items, limit)
    names = tuple(proposal.name for proposal, _ in selected)
    if not confirm:
        return ReconcileResult(False, candidates, names, (), tuple(skipped))
    reconciled, conflicts = _replace_reconciliation_candidates(data_path, selected)
    return ReconcileResult(True, candidates, names, tuple(reconciled), (*skipped, *conflicts))


def _select_reconciliation_candidates(
    data_path: Path, items: list[AttentionItem], limit: int
) -> tuple[list[tuple[OutboxProposal, AttentionItem]], list[ReconcileSkip], int]:
    """Select fresh invalid-command records and explain intentional omissions."""
    current_items = {item.item_id: item for item in items}
    candidates = [
        proposal
        for proposal in list_proposals(data_path)
        if proposal.version == 2
        and proposal.decision_provenance == "llm"
        and proposal.fallback_category == "invalid-command"
        and proposal.command_argv is None
        and proposal.item_id is not None
    ]
    selected: list[tuple[OutboxProposal, AttentionItem]] = []
    skipped: list[ReconcileSkip] = []
    for proposal in candidates:
        item = current_items.get(proposal.item_id or "")
        if item is None:
            skipped.append(ReconcileSkip(proposal.name, "stale"))
        elif propose_for(item).command_argv is None:
            skipped.append(ReconcileSkip(proposal.name, "no-baseline"))
        elif len(selected) < limit:
            selected.append((proposal, item))
    return selected, skipped, len(candidates)


def _replace_reconciliation_candidates(
    data_path: Path, selected: list[tuple[OutboxProposal, AttentionItem]]
) -> tuple[list[str], list[ReconcileSkip]]:
    """Replace selected records and retain concurrent changes as conflicts."""
    reconciled: list[str] = []
    conflicts: list[ReconcileSkip] = []
    for proposal, item in selected:
        if replace_invalid_command_proposal(data_path, proposal, item, propose_for(item)) == "reconciled":
            reconciled.append(proposal.name)
        else:
            conflicts.append(ReconcileSkip(proposal.name, "conflict"))
    return reconciled, conflicts
