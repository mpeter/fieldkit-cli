"""Public contracts for versioned companion proposal envelopes (implementation change)."""

import hashlib
import json
import os
import stat
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from fieldkit.companion.decide import ProposedAction
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.outbox import (
    claim_proposal,
    list_proposals,
    load_proposal,
    lock_proposal_for_retention,
    proposal_item_ids,
    replace_invalid_command_proposal,
    write_proposal,
)

pytestmark = pytest.mark.unit


def _item(*, item_id: str = "0123456789abcdef", observed_at: str = "2026-09-10") -> AttentionItem:
    return AttentionItem(
        item_id, "watch/pursuit-stalls", "acme", "warning", "Review", "evidence.md", "grill", observed_at
    )


def _action(command: tuple[str, ...] | None = ("pursuit", "health", "--account", "acme", "--json")) -> ProposedAction:
    return ProposedAction("grill", "Review the account", None, command)


def _valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "item_id": "0123456789abcdef",
        "created_at": "2026-09-10T12:00:00+00:00",
        "command_argv": ["gtask", "complete", "task-1", "--confirm"],
        "markdown": "# Proposal\n",
    }
    payload.update(changes)
    return payload


def _write_raw(data_path: Path, name: str, payload: object) -> Path:
    directory = data_path / "companion-outbox"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = directory / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_version_two_decision_fields_round_trip_and_remain_approvable(tmp_path: Path) -> None:
    action = ProposedAction(
        "grill",
        "The close date is stale.",
        None,
        ("pursuit", "advance", "acme", "--dry-run"),
        recommendation="Preview the stage change.",
    )

    target = write_proposal(
        tmp_path,
        _item(),
        action,
        decision_provenance="llm",
        fallback_category=None,
    )

    proposal = load_proposal(tmp_path, target.name)
    assert proposal is not None
    assert proposal.version == 2
    assert proposal.recommendation == "Preview the stage change."
    assert proposal.decision_provenance == "llm"
    assert proposal.fallback_category is None
    assert proposal.approvable is True
    assert "## Recommendation" in proposal.markdown


def test_version_two_accepts_deterministic_invalid_command_fallback_with_baseline(tmp_path: Path) -> None:
    action = ProposedAction(
        "grill",
        "The close date is stale.",
        None,
        ("pursuit", "health", "--account", "acme", "--json"),
        recommendation="Review the pursuit.",
    )

    target = write_proposal(
        tmp_path,
        _item(),
        action,
        decision_provenance="deterministic",
        fallback_category="invalid-command",
    )

    proposal = load_proposal(tmp_path, target.name)

    assert proposal is not None
    assert proposal.approvable is True
    assert proposal.decision_provenance == "deterministic"
    assert proposal.fallback_category == "invalid-command"


def test_reconcile_replaces_matching_invalid_command_envelope_atomically(tmp_path: Path) -> None:
    payload = _valid_payload(
        version=2,
        command_argv=None,
        recommendation="Unsafe recommendation.",
        decision_provenance="llm",
        fallback_category="invalid-command",
    )
    target = _write_raw(tmp_path, "legacy.proposal.json", payload)
    candidate = load_proposal(tmp_path, target.name)

    assert candidate is not None
    result = replace_invalid_command_proposal(tmp_path, candidate, _item(), _action())
    reconciled = load_proposal(tmp_path, target.name)

    assert result == "reconciled"
    assert reconciled is not None
    assert reconciled.approvable is True
    assert reconciled.command_argv == ("pursuit", "health", "--account", "acme", "--json")
    assert reconciled.decision_provenance == "deterministic"
    assert reconciled.fallback_category == "invalid-command"


def test_reconcile_preserves_replaced_generation(tmp_path: Path) -> None:
    payload = _valid_payload(
        version=2,
        command_argv=None,
        recommendation="Unsafe recommendation.",
        decision_provenance="llm",
        fallback_category="invalid-command",
    )
    target = _write_raw(tmp_path, "legacy.proposal.json", payload)
    candidate = load_proposal(tmp_path, target.name)
    replacement = target.parent / "replacement.json"
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    replacement.replace(target)

    assert candidate is not None
    assert replace_invalid_command_proposal(tmp_path, candidate, _item(), _action()) == "conflict"
    assert json.loads(target.read_text(encoding="utf-8"))["command_argv"] is None


@pytest.mark.parametrize(
    ("name", "payload", "item", "action", "expected"),
    [
        ("../legacy.proposal.json", _valid_payload(), _item(), _action(), "not-reconcilable"),
        ("legacy.proposal.json", _valid_payload(), _item(), _action(None), "not-reconcilable"),
        (
            "legacy.proposal.json",
            _valid_payload(
                version=2,
                command_argv=None,
                recommendation="Unsafe",
                decision_provenance="llm",
                fallback_category="invalid-command",
            ),
            _item(item_id="fedcba9876543210"),
            _action(),
            "not-reconcilable",
        ),
    ],
)
def test_reconcile_rejects_unsafe_or_mismatched_candidates(
    tmp_path: Path,
    name: str,
    payload: dict[str, object],
    item: AttentionItem,
    action: ProposedAction,
    expected: str,
) -> None:
    """Replacement rejects unsafe paths and envelopes that no longer match."""
    target = _write_raw(tmp_path, "legacy.proposal.json", payload)
    candidate = load_proposal(tmp_path, target.name)

    assert candidate is not None
    assert replace_invalid_command_proposal(tmp_path, replace(candidate, name=name), item, action) == expected


def test_version_two_rejects_contradictory_llm_provider_fallback(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload.update(
        version=2,
        recommendation="Review the pursuit.",
        decision_provenance="llm",
        fallback_category="auth",
    )
    target = _write_raw(tmp_path, "contradictory.proposal.json", payload)

    proposal = load_proposal(tmp_path, target.name)

    assert proposal is not None
    assert proposal.approvable is False
    assert "llm provenance permits only" in (proposal.validation_error or "")


def test_version_two_rejects_missing_recommendation(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload.update(
        version=2,
        recommendation=None,
        decision_provenance="llm",
        fallback_category=None,
    )
    target = _write_raw(tmp_path, "missing-recommendation.proposal.json", payload)

    proposal = load_proposal(tmp_path, target.name)

    assert proposal is not None
    assert proposal.approvable is False
    assert "recommendation must be" in (proposal.validation_error or "")


def test_claim_installs_marker_and_failure_restores_original(tmp_path: Path) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload())
    original = target.read_text(encoding="utf-8")
    claim = claim_proposal(tmp_path, target.name)
    try:
        assert claim.status == "claimed"
        marker = json.loads(target.read_text(encoding="utf-8"))
        assert marker["command_argv"] is None
        assert marker["markdown"].startswith("Execution in progress or outcome unknown")
        assert claim.finish(success=False) is True
        assert claim.finish(success=False) is False
    finally:
        claim.close()
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(original)


def test_claim_success_deletes_and_second_claim_is_missing(tmp_path: Path) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload())
    claim = claim_proposal(tmp_path, target.name)
    try:
        assert claim.status == "claimed"
        assert claim.finish(success=True) is True
    finally:
        claim.close()
    assert claim_proposal(tmp_path, target.name).status == "missing"


def test_interrupted_retention_lock_leaves_original_proposal_approvable(tmp_path: Path) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload())
    original = target.read_bytes()

    retention = lock_proposal_for_retention(tmp_path, target.name)
    assert retention.status == "locked"
    retention.close()

    assert target.read_bytes() == original
    loaded = load_proposal(tmp_path, target.name)
    assert loaded is not None
    assert loaded.approvable is True
    retry = lock_proposal_for_retention(tmp_path, target.name)
    try:
        assert retry.finish() is True
    finally:
        retry.close()
    assert not target.exists()


def test_retention_lock_reports_nonretirable_and_missing_states(tmp_path: Path) -> None:
    unsafe_name = "unsafe.proposal.json"
    unsafe = _write_raw(tmp_path, unsafe_name, _valid_payload(command_argv=None))

    invalid_name = lock_proposal_for_retention(tmp_path, "../outside.proposal.json")
    assert invalid_name.status == "not-retirable"
    missing = lock_proposal_for_retention(tmp_path, "missing.proposal.json")
    assert missing.status == "missing"
    nonretirable = lock_proposal_for_retention(tmp_path, unsafe.name)
    assert nonretirable.status == "not-retirable"
    assert unsafe.exists()


def test_claim_rejects_null_argv_and_unsafe_name(tmp_path: Path) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload(command_argv=None))
    assert claim_proposal(tmp_path, target.name).status == "not-approvable"
    assert claim_proposal(tmp_path, "../task.proposal.json").status == "not-approvable"


def test_writer_rejects_symlink_lock(tmp_path: Path) -> None:
    target_name = "2026-09-10-0123456789abcdef-grill.proposal.json"
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    lock_name = ".lock-" + hashlib.sha256(target_name.encode()).hexdigest()[:16]
    (directory / lock_name).symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        write_proposal(tmp_path, _item(), _action())


def test_same_name_writer_blocks_until_claim_finishes_then_survives(tmp_path: Path) -> None:
    target = write_proposal(tmp_path, _item(), _action(("gtask", "complete", "old", "--confirm")))
    claim = claim_proposal(tmp_path, target.name)
    assert claim.status == "claimed"
    started = threading.Event()
    finished = threading.Event()

    def publish() -> None:
        started.set()
        write_proposal(tmp_path, _item(), _action(("gtask", "complete", "new", "--confirm")))
        finished.set()

    thread = threading.Thread(target=publish)
    thread.start()
    assert started.wait(timeout=1)
    assert not finished.wait(timeout=0.1)
    assert claim.finish(success=True) is True
    claim.close()
    thread.join(timeout=2)
    assert finished.is_set()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["command_argv"] == ["gtask", "complete", "new", "--confirm"]


def test_replacement_after_marker_publication_is_never_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload())
    replacement = _valid_payload(item_id="fedcba9876543210", command_argv=["gtask", "complete", "new", "--confirm"])
    from fieldkit.companion import outbox as outbox_mod

    original_write = outbox_mod._write_atomic

    def replace_after_publish(directory_fd: int, filename: str, payload: dict[str, object]) -> tuple[int, int]:
        identity = original_write(directory_fd, filename, payload)
        if payload["command_argv"] is None:
            original_write(directory_fd, filename, replacement)
        return identity

    monkeypatch.setattr(outbox_mod, "_write_atomic", replace_after_publish)
    claim = claim_proposal(tmp_path, target.name)
    try:
        assert claim.status == "claimed"
        assert claim.finish(success=True) is False
    finally:
        claim.close()
    assert json.loads(target.read_text(encoding="utf-8"))["item_id"] == "fedcba9876543210"


def test_replacement_before_marker_verify_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write_raw(tmp_path, "task.proposal.json", _valid_payload())
    replacement = target.parent / "replacement.tmp"
    replacement.write_text(json.dumps(_valid_payload(item_id="fedcba9876543210")), encoding="utf-8")
    from fieldkit.companion import outbox as outbox_mod

    original_verify = outbox_mod._verify_published

    def replace_before_verify(directory_fd: int, filename: str, expected: tuple[int, int]) -> tuple[int, int]:
        os.replace("replacement.tmp", filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return original_verify(directory_fd, filename, expected)

    monkeypatch.setattr(outbox_mod, "_verify_published", replace_before_verify)
    with pytest.raises(OSError, match="not published"):
        claim_proposal(tmp_path, target.name)
    assert json.loads(target.read_text(encoding="utf-8"))["item_id"] == "fedcba9876543210"


def test_write_proposal_publishes_exact_atomic_envelope(tmp_path: Path) -> None:
    result = write_proposal(tmp_path, _item(), _action(), enrichment_output="context")
    assert result.name == "2026-09-10-0123456789abcdef-grill.proposal.json"

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "item_id", "created_at", "command_argv", "markdown"}
    assert payload["version"] == 1
    assert payload["item_id"] == "0123456789abcdef"
    assert payload["command_argv"] == ["pursuit", "health", "--account", "acme", "--json"]
    assert "context" in payload["markdown"]
    assert datetime.fromisoformat(payload["created_at"]).utcoffset() is not None
    assert result.read_bytes().endswith(b"\n")
    assert not list(result.parent.glob(".proposal-*.tmp"))


def test_replacing_basename_keeps_markdown_and_argv_in_one_generation(tmp_path: Path) -> None:
    target = write_proposal(tmp_path, _item(), _action(("pursuit", "health")), enrichment_output="old")
    write_proposal(tmp_path, _item(), _action(("watch", "logs", "pursuit-stalls")), enrichment_output="new")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["command_argv"] == ["watch", "logs", "pursuit-stalls"]
    assert "new" in payload["markdown"]
    assert "old" not in payload["markdown"]


@pytest.mark.parametrize("observed_at", ["2026-09-10", "2026-09-10T12:00:00+00:00"])
def test_write_preserves_valid_observation_date(tmp_path: Path, observed_at: str) -> None:
    result = write_proposal(tmp_path, _item(observed_at=observed_at), _action())
    assert result.name.startswith("2026-09-10-")


def test_traversal_observation_falls_back_without_escape(tmp_path: Path) -> None:
    result = write_proposal(tmp_path, _item(observed_at="../../outside/ownedT12:00:00Z"), _action())
    assert result.name.startswith("undated-")
    assert result.parent == tmp_path / "companion-outbox"
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize(
    ("item", "action"),
    [
        (_item(item_id="bad/id"), _action()),
        (_item(), _action(())),
        (_item(), _action(("gws", "tasks"))),
        (_item(), _action(("fieldkit", "gtask"))),
        (_item(), _action(("/bin/sh",))),
        (_item(), _action(("pursuit", ""))),
    ],
)
def test_invalid_writer_data_publishes_nothing(tmp_path: Path, item: AttentionItem, action: ProposedAction) -> None:
    with pytest.raises(ValueError):
        write_proposal(tmp_path, item, action)
    assert list_proposals(tmp_path) == []
    assert not (tmp_path / "companion-outbox").exists()


def test_null_argv_is_valid_but_not_approvable(tmp_path: Path) -> None:
    target = write_proposal(tmp_path, _item(), _action(None))
    result = load_proposal(tmp_path, target.name)
    assert result is not None
    assert result.command_argv is None
    assert result.approvable is False
    assert result.validation_error is None


def test_legacy_markdown_is_readable_but_not_approvable(tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    target = directory / "2026-09-10-0123456789abcdef-legacy.md"
    target.write_text("# Legacy\n", encoding="utf-8")

    result = load_proposal(tmp_path, target.name)
    assert result is not None
    assert result.markdown == "# Legacy\n"
    assert result.item_id == "0123456789abcdef"
    assert result.approvable is False
    assert proposal_item_ids(tmp_path) == {"0123456789abcdef"}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 1},
        _valid_payload(extra=True),
        _valid_payload(version=2),
        _valid_payload(version=True),
        _valid_payload(item_id="BAD"),
        _valid_payload(created_at="2026-09-10"),
        _valid_payload(created_at=42),
        _valid_payload(created_at="not-a-timestamp"),
        _valid_payload(command_argv=[]),
        _valid_payload(command_argv="gtask"),
        _valid_payload(command_argv=["gws", "tasks"]),
        _valid_payload(command_argv=["gtask", ""]),
        _valid_payload(markdown=" "),
    ],
)
def test_invalid_envelope_is_visible_diagnostic_and_does_not_retire(tmp_path: Path, payload: object) -> None:
    target = _write_raw(tmp_path, "2026-09-10-0123456789abcdef-bad.proposal.json", payload)
    result = load_proposal(tmp_path, target.name)
    assert result is not None
    assert result.approvable is False
    assert result.item_id is None
    assert result.validation_error is not None
    assert result.markdown.startswith("Invalid proposal envelope:")
    assert proposal_item_ids(tmp_path) == set()


def test_non_json_envelope_does_not_expose_input(tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    target = directory / "bad.proposal.json"
    target.write_text("secret attacker markdown", encoding="utf-8")
    result = load_proposal(tmp_path, target.name)
    assert result is not None
    assert "secret attacker markdown" not in result.markdown


def test_listing_orders_mtime_descending_then_name(tmp_path: Path) -> None:
    first = _write_raw(tmp_path, "b.proposal.json", _valid_payload(item_id="1111111111111111"))
    second = _write_raw(tmp_path, "a.proposal.json", _valid_payload(item_id="2222222222222222"))
    os.utime(first, (100, 100))
    os.utime(second, (100, 100))
    newer = _write_raw(tmp_path, "c.proposal.json", _valid_payload(item_id="3333333333333333"))
    os.utime(newer, (200, 200))
    result = list_proposals(tmp_path)
    assert [proposal.name for proposal in result] == ["c.proposal.json", "a.proposal.json", "b.proposal.json"]


@pytest.mark.parametrize("name", ["../secret.md", "proposal.txt", "/tmp/x.proposal.json"])
def test_load_rejects_non_outbox_names(tmp_path: Path, name: str) -> None:
    assert load_proposal(tmp_path, name) is None


def test_symlink_root_and_entry_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "companion-outbox").symlink_to(outside, target_is_directory=True)
    assert list_proposals(data) == []
    with pytest.raises(OSError):
        write_proposal(data, _item(), _action())

    (data / "companion-outbox").unlink()
    (data / "companion-outbox").mkdir(mode=0o700)
    external = outside / "external.proposal.json"
    external.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    (data / "companion-outbox" / "linked.proposal.json").symlink_to(external)
    assert list_proposals(data) == []


def test_symlink_data_root_fails_closed(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    real_data.mkdir(mode=0o700)
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    assert list_proposals(linked_data) == []
    with pytest.raises(OSError):
        write_proposal(linked_data, _item(), _action())
    assert not (real_data / "companion-outbox").exists()


def test_reader_fails_closed_when_outbox_metadata_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "companion-outbox").mkdir(mode=0o700)

    def fail_fstat(_fd: int) -> os.stat_result:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(os, "fstat", fail_fstat)
    assert list_proposals(tmp_path) == []


def test_reader_does_not_swallow_control_flow_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "companion-outbox").mkdir(mode=0o700)

    def interrupt_fstat(_fd: int) -> os.stat_result:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fstat", interrupt_fstat)
    with pytest.raises(KeyboardInterrupt):
        list_proposals(tmp_path)


def test_reader_fails_closed_when_outbox_cannot_be_enumerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "companion-outbox").mkdir(mode=0o700)

    def fail_listdir(_fd: int) -> list[str]:
        raise OSError("directory unavailable")

    monkeypatch.setattr(os, "listdir", fail_listdir)
    assert list_proposals(tmp_path) == []


def test_writer_tightens_legacy_outbox_permissions_without_hiding_legacy_proposals(tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o755)
    legacy = directory / "2026-09-09-abcdef0123456789-legacy.md"
    legacy.write_text("# Legacy proposal\n", encoding="utf-8")

    assert [proposal.name for proposal in list_proposals(tmp_path)] == [legacy.name]
    write_proposal(tmp_path, _item(), _action())
    assert stat_mode(directory) == 0o700
    assert legacy.read_text(encoding="utf-8") == "# Legacy proposal\n"


def test_group_writable_outbox_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    directory.chmod(0o770)

    assert list_proposals(tmp_path) == []
    with pytest.raises(OSError, match="operator-owned directory"):
        write_proposal(tmp_path, _item(), _action())


def test_writer_propagates_outbox_metadata_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_fstat = os.fstat

    def fail_outbox_fstat(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            raise OSError("metadata unavailable")
        return info

    monkeypatch.setattr(os, "fstat", fail_outbox_fstat)
    with pytest.raises(OSError, match="metadata unavailable"):
        write_proposal(tmp_path, _item(), _action())


def test_writer_propagates_legacy_permission_migration_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    def fail_fchmod(_fd: int, _mode: int) -> None:
        raise OSError("permission migration failed")

    monkeypatch.setattr(os, "fchmod", fail_fchmod)
    with pytest.raises(OSError, match="permission migration failed"):
        write_proposal(tmp_path, _item(), _action())
    assert list(directory.iterdir()) == []


def test_writer_closes_outbox_when_temp_name_generation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened_outbox_fds: list[int] = []
    closed_fds: list[int] = []
    original_open = os.open
    original_close = os.close

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "companion-outbox":
            opened_outbox_fds.append(fd)
        return fd

    def record_close(fd: int) -> None:
        closed_fds.append(fd)
        original_close(fd)

    def fail_token_hex(_length: int) -> str:
        raise OSError("entropy unavailable")

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr("fieldkit.companion.outbox.secrets.token_hex", fail_token_hex)

    with pytest.raises(OSError, match="entropy unavailable"):
        write_proposal(tmp_path, _item(), _action())
    assert opened_outbox_fds
    assert opened_outbox_fds[-1] in closed_fds


def test_fifo_is_skipped_without_blocking(tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    os.mkfifo(directory / "blocked.proposal.json")
    assert list_proposals(tmp_path) == []


def test_first_writer_creates_private_outbox(tmp_path: Path) -> None:
    result = write_proposal(tmp_path, _item(), _action())
    assert result.exists()
    assert stat_mode(result.parent) == 0o700


def test_benign_first_writer_race_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    original_mkdir = os.mkdir
    raced = False

    def mkdir(path: str | bytes | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        nonlocal raced
        if path == "companion-outbox" and dir_fd is not None and not raced:
            raced = True
            original_mkdir(path, 0o700, dir_fd=dir_fd)
            raise FileExistsError
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir)
    result = write_proposal(data, _item(), _action())
    assert result.exists()


def test_entry_swapped_to_symlink_between_list_and_open_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_raw(tmp_path, "swap.proposal.json", _valid_payload())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    original_open = os.open
    swapped = False

    def open_file(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_file)
    assert list_proposals(tmp_path) == []


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
