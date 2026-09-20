"""Proposal retention and lifecycle evidence contracts (implementation change)."""

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.companion.cli import cli
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.lifecycle import (
    ignored_rate,
    prune_proposals,
    read_outcomes,
    reconcile_invalid_command_proposals,
    record_outcome,
)
from fieldkit.companion.outbox import list_proposals
from fieldkit.companion.suppress import active_cooldowns

pytestmark = pytest.mark.unit
_NOW = dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)


def _proposal(data_path: Path, name: str, *, created_at: str, approvable: bool = True) -> Path:
    directory = data_path / "companion-outbox"
    directory.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "version": 1,
        "item_id": name.split("-")[0],
        "created_at": created_at,
        "command_argv": ["pursuit", "health", "--json"] if approvable else None,
        "markdown": "# Proposal\n",
    }
    target = directory / f"{name}.proposal.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _invalid_command_proposal(data_path: Path, item_id: str, *, name: str = "legacy.proposal.json") -> Path:
    directory = data_path / "companion-outbox"
    directory.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "version": 2,
        "item_id": item_id,
        "created_at": "2026-09-10T12:00:00+00:00",
        "command_argv": None,
        "markdown": "# Legacy\n",
        "recommendation": "Unsafe recommendation.",
        "decision_provenance": "llm",
        "fallback_category": "invalid-command",
    }
    target = directory / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _item(item_id: str) -> AttentionItem:
    return AttentionItem(
        item_id, "watch/pursuit-stalls", "acme", "warning", "Review", "evidence.md", "grill", "2026-09-10"
    )


def test_preview_selects_oldest_valid_batch_without_writes(tmp_path: Path) -> None:
    oldest = _proposal(tmp_path, "1111111111111111-oldest", created_at="2026-08-01T12:00:00+00:00")
    second = _proposal(tmp_path, "2222222222222222-second", created_at="2026-08-02T12:00:00+00:00")
    fresh = _proposal(tmp_path, "3333333333333333-fresh", created_at="2026-09-09T12:00:00+00:00")
    claimed = _proposal(tmp_path, "4444444444444444-claimed", created_at="2026-08-03T12:00:00+00:00", approvable=False)
    legacy = tmp_path / "companion-outbox" / "2026-08-01-5555555555555555-legacy.md"
    legacy.write_text("# Legacy\n", encoding="utf-8")
    invalid = tmp_path / "companion-outbox" / "invalid.proposal.json"
    invalid.write_text("{}", encoding="utf-8")
    paths = (oldest, second, fresh, claimed, legacy, invalid)
    before = {path.name: path.read_bytes() for path in paths}

    result = prune_proposals(tmp_path, limit=1, now=_NOW)

    assert result.confirmed is False
    assert result.selected == (oldest.name,)
    assert result.eligible == 2
    assert result.remaining_eligible == 2
    assert {skip.name for skip in result.skipped} == {claimed.name, legacy.name, invalid.name}
    assert {path.name: path.read_bytes() for path in paths} == before
    assert not list(tmp_path.glob("companion-proposal-outcomes-*.jsonl"))


def test_confirm_retires_bounded_oldest_and_transitions_to_cooldown(tmp_path: Path) -> None:
    oldest = _proposal(tmp_path, "1111111111111111-oldest", created_at="2026-08-01T12:00:00+00:00")
    second = _proposal(tmp_path, "2222222222222222-second", created_at="2026-08-02T12:00:00+00:00")

    result = prune_proposals(tmp_path, limit=1, confirm=True, now=_NOW)

    assert result.retired == (oldest.name,)
    assert result.remaining_eligible == 1
    assert not oldest.exists()
    assert second.exists()
    assert active_cooldowns(tmp_path, now=_NOW) == {"1111111111111111"}
    assert active_cooldowns(tmp_path, now=_NOW + dt.timedelta(hours=24)) == set()
    outcomes = read_outcomes(tmp_path)
    assert [(row.proposal_name, row.item_id, row.outcome) for row in outcomes] == [
        (oldest.name, "1111111111111111", "expired")
    ]
    assert result.ignored_rate == 1.0


def test_reconcile_preview_and_confirm_replace_only_fresh_invalid_command_proposal(tmp_path: Path) -> None:
    target = _invalid_command_proposal(tmp_path, "1111111111111111")
    preview = reconcile_invalid_command_proposals(tmp_path, [_item("1111111111111111")])
    result = reconcile_invalid_command_proposals(tmp_path, [_item("1111111111111111")], confirm=True)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert preview.confirmed is False
    assert preview.selected == (target.name,)
    assert result.reconciled == (target.name,)
    assert payload["command_argv"] == ["pursuit", "health", "--account", "acme", "--json"]
    assert payload["decision_provenance"] == "deterministic"


def test_reconcile_preserves_invalid_command_proposal_without_fresh_item(tmp_path: Path) -> None:
    target = _invalid_command_proposal(tmp_path, "1111111111111111")
    before = target.read_bytes()

    result = reconcile_invalid_command_proposals(tmp_path, [])

    assert result.selected == ()
    assert result.skipped[0].to_dict() == {"name": target.name, "reason": "stale"}
    assert target.read_bytes() == before


def test_reconcile_skips_no_baseline_and_obeys_limit(tmp_path: Path) -> None:
    """Preview retains records without a safe baseline and respects the batch cap."""
    no_baseline = _invalid_command_proposal(tmp_path, "1111111111111111")
    _invalid_command_proposal(tmp_path, "2222222222222222", name="limited.proposal.json")
    no_baseline_item = replace(_item("1111111111111111"), source="run-status/unknown-watcher")

    result = reconcile_invalid_command_proposals(tmp_path, [no_baseline_item, _item("2222222222222222")], limit=1)

    assert result.candidates == 2
    assert result.selected == ("limited.proposal.json",)
    assert result.skipped[0].to_dict() == {"name": no_baseline.name, "reason": "no-baseline"}


def test_reconcile_cli_previews_json_and_confirms_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI preview and confirmation expose the bounded reconciliation result."""
    target = _invalid_command_proposal(tmp_path, "1111111111111111")
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.companion.feed.get_feed", lambda *_args, **_kwargs: [_item("1111111111111111")])

    preview = CliRunner().invoke(cli, ["reconcile-invalid", "--json"])
    confirmed = CliRunner().invoke(cli, ["reconcile-invalid", "--confirm"])

    assert preview.exit_code == 0
    assert json.loads(preview.output)["selected"] == [target.name]
    assert confirmed.exit_code == 0
    assert target.name in confirmed.output
    assert "reconciled 1 proposal(s); skipped=0" in confirmed.output


def test_reconcile_cli_rejects_confirm_and_dry_run() -> None:
    """CLI rejects mutually exclusive reconciliation execution modes."""
    invocation = CliRunner().invoke(cli, ["reconcile-invalid", "--confirm", "--dry-run"])

    assert invocation.exit_code == 3
    assert "cannot be combined with --confirm" in invocation.output


def test_outcome_statistics_deduplicate_retry_by_proposal_name(tmp_path: Path) -> None:
    first_path = record_outcome(
        tmp_path,
        proposal_name="first.proposal.json",
        item_id="1111111111111111",
        outcome="expired",
        when=_NOW,
    )
    assert first_path == tmp_path / "companion-proposal-outcomes-2026-09.jsonl"
    record_outcome(
        tmp_path,
        proposal_name="first.proposal.json",
        item_id="1111111111111111",
        outcome="expired",
        when=_NOW,
    )
    record_outcome(
        tmp_path,
        proposal_name="second.proposal.json",
        item_id="2222222222222222",
        outcome="approved",
        when=_NOW,
    )

    result = ignored_rate(tmp_path)

    assert result == 0.5
    assert (tmp_path / "companion-proposal-outcomes-2026-09.jsonl").stat().st_mode & 0o777 == 0o600


def test_confirm_reports_claim_conflict_without_deleting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _proposal(tmp_path, "1111111111111111-old", created_at="2026-08-01T12:00:00+00:00")
    from fieldkit.companion import lifecycle

    class ConflictRetention:
        status = "conflict"
        proposal = None

        def finish(self) -> bool:
            raise AssertionError

        def close(self) -> None:
            return None

    monkeypatch.setattr(lifecycle, "lock_proposal_for_retention", lambda *_args: ConflictRetention())

    result = prune_proposals(tmp_path, confirm=True, now=_NOW)

    assert result.retired == ()
    assert result.skipped[-1].to_dict() == {"name": target.name, "reason": "conflict"}
    assert target.exists()
    assert active_cooldowns(tmp_path, now=_NOW) == set()


def test_confirm_preserves_replacement_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _proposal(tmp_path, "1111111111111111-old", created_at="2026-08-01T12:00:00+00:00")
    replacement = target.parent / "replacement.tmp"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["markdown"] = "# Corrected proposal\n"
    replacement.write_text(json.dumps(payload), encoding="utf-8")

    def list_then_replace(data_path: Path):
        selected = list_proposals(data_path)
        replacement.replace(target)
        return selected

    monkeypatch.setattr("fieldkit.companion.lifecycle.list_proposals", list_then_replace)

    result = prune_proposals(tmp_path, confirm=True, now=_NOW)

    assert result.retired == ()
    assert result.skipped[-1].to_dict() == {"name": target.name, "reason": "conflict"}
    assert json.loads(target.read_text(encoding="utf-8"))["markdown"] == "# Corrected proposal\n"
    assert active_cooldowns(tmp_path, now=_NOW) == set()


def test_cli_json_preview_uses_configured_data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _proposal(tmp_path, "1111111111111111-old", created_at="2026-08-01T12:00:00+00:00")
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)

    invocation = CliRunner().invoke(cli, ["prune", "--json"])

    assert invocation.exit_code == 0
    payload = json.loads(invocation.output)
    assert payload["selected"] == [target.name]
    assert payload["confirmed"] is False
    assert target.exists()


def test_cli_confirm_json_retires_selected_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _proposal(tmp_path, "1111111111111111-old", created_at="2026-08-01T12:00:00+00:00")
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)

    invocation = CliRunner().invoke(cli, ["prune", "--confirm", "--json"])

    assert invocation.exit_code == 0
    payload = json.loads(invocation.output)
    assert payload["retired"] == [target.name]
    assert payload["ignored_rate"] == 1.0
    assert not target.exists()


def test_cli_explicit_dry_run_preserves_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _proposal(tmp_path, "1111111111111111-old", created_at="2026-08-01T12:00:00+00:00")
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)

    invocation = CliRunner().invoke(cli, ["prune", "--dry-run", "--json"])

    assert invocation.exit_code == 0
    payload = json.loads(invocation.output)
    assert payload["confirmed"] is False
    assert payload["selected"] == [target.name]
    assert target.exists()


@pytest.mark.parametrize(
    ("older_than", "limit", "message"),
    [
        (dt.timedelta(0), 25, "older_than must be positive"),
        (dt.timedelta(days=14), 0, "limit must be between"),
        (dt.timedelta(days=14), 101, "limit must be between"),
    ],
)
def test_domain_rejects_invalid_bounds(older_than: dt.timedelta, limit: int, message: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=message):
        prune_proposals(tmp_path, older_than=older_than, limit=limit)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--older-than", "0d"], "positive whole-day"),
        (["--older-than", "14h"], "positive whole-day"),
        (["--limit", "0"], "0 is not in the range"),
        (["--limit", "101"], "101 is not in the range"),
        (["--confirm", "--dry-run"], "cannot be combined"),
    ],
)
def test_cli_rejects_invalid_retention_bounds(
    args: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["companion", "prune", *args])

    assert exit_code == 3
    assert message in capsys.readouterr().err


def test_outcome_reader_ignores_malformed_lines_but_rejects_public_file(tmp_path: Path) -> None:
    path = record_outcome(
        tmp_path,
        proposal_name="first.proposal.json",
        item_id="1111111111111111",
        outcome="approved",
        when=_NOW,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    assert len(read_outcomes(tmp_path)) == 1

    path.chmod(0o644)
    with pytest.raises(OSError, match="private regular file"):
        read_outcomes(tmp_path)


def test_short_outcome_write_completes_record_and_preserves_next_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    short_once = True

    def short_write(file_fd: int, data: bytes | memoryview) -> int:
        nonlocal short_once
        if short_once:
            short_once = False
            prefix = bytes(data[: max(1, len(data) // 2)])
            return real_write(file_fd, prefix)
        return real_write(file_fd, data)

    monkeypatch.setattr("fieldkit.companion.lifecycle.os.write", short_write)
    first = record_outcome(
        tmp_path,
        proposal_name="first.proposal.json",
        item_id="1111111111111111",
        outcome="expired",
        when=_NOW,
    )
    assert first == tmp_path / "companion-proposal-outcomes-2026-09.jsonl"
    second = record_outcome(
        tmp_path,
        proposal_name="second.proposal.json",
        item_id="2222222222222222",
        outcome="approved",
        when=_NOW,
    )
    assert second == first
    assert [(row.proposal_name, row.outcome) for row in read_outcomes(tmp_path)] == [
        ("first.proposal.json", "expired"),
        ("second.proposal.json", "approved"),
    ]


def test_failed_partial_outcome_append_rolls_back_before_next_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    calls = 0

    def short_then_error(file_fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(file_fd, bytes(data[: max(1, len(data) // 2)]))
        raise OSError("storage failed")

    monkeypatch.setattr("fieldkit.companion.lifecycle.os.write", short_then_error)
    with pytest.raises(OSError, match="storage failed"):
        record_outcome(
            tmp_path,
            proposal_name="failed.proposal.json",
            item_id="1111111111111111",
            outcome="expired",
            when=_NOW,
        )
    path = tmp_path / "companion-proposal-outcomes-2026-09.jsonl"
    assert path.read_bytes() == b""

    monkeypatch.setattr("fieldkit.companion.lifecycle.os.write", real_write)
    recovered = record_outcome(
        tmp_path,
        proposal_name="recovered.proposal.json",
        item_id="2222222222222222",
        outcome="approved",
        when=_NOW,
    )
    assert recovered == path
    assert [(row.proposal_name, row.outcome) for row in read_outcomes(tmp_path)] == [
        ("recovered.proposal.json", "approved")
    ]


def test_outcome_journal_rejects_symlinked_data_root(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    with pytest.raises(OSError):
        record_outcome(
            linked_data,
            proposal_name="first.proposal.json",
            item_id="1111111111111111",
            outcome="expired",
            when=_NOW,
        )
    with pytest.raises(OSError):
        read_outcomes(linked_data)
    assert list(real_data.iterdir()) == []


def test_outcome_journal_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "companion-proposal-outcomes-2026-09.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(OSError):
        record_outcome(
            tmp_path,
            proposal_name="first.proposal.json",
            item_id="1111111111111111",
            outcome="expired",
            when=_NOW,
        )
    with pytest.raises(OSError, match="private regular file"):
        read_outcomes(tmp_path)
