"""Tests for fieldkit.companion.suppress — what the feed currently withholds.

The point of this module is that retirement is never permanent and never a
side effect of recording evidence. These tests lock both properties.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fieldkit.companion.suppress import (
    DEFAULT_COOLDOWN,
    active_cooldowns,
    add_cooldown,
    retired_item_ids,
    suppress_path,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_cooldown_suppresses_then_expires(tmp_path: Path) -> None:
    """A cooldown withholds an item, then stops — without any cleanup step.

    This is the whole reason cooldowns replaced journal suppression: a stalled
    pursuit is a standing condition, so retiring it forever because it was once
    observed is wrong. It must come back on its own.
    """
    add_cooldown(tmp_path, "abc123", reason="read:alert/pursuit-stalls", now=_NOW)

    during = active_cooldowns(tmp_path, now=_NOW + timedelta(hours=1))
    assert during == {"abc123"}

    after = active_cooldowns(tmp_path, now=_NOW + DEFAULT_COOLDOWN + timedelta(seconds=1))
    assert after == set()


@pytest.mark.unit
def test_add_cooldown_prunes_expired_entries(tmp_path: Path) -> None:
    """Expired entries are dropped on write so the file cannot grow unbounded."""
    add_cooldown(tmp_path, "old", reason="read:x", now=_NOW)
    add_cooldown(tmp_path, "new", reason="read:y", now=_NOW + DEFAULT_COOLDOWN + timedelta(hours=1))

    stored = json.loads(suppress_path(tmp_path).read_text(encoding="utf-8"))

    assert set(stored) == {"new"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        "{not json",  # corrupt
        '{"abc": {"until": "not-a-date"}}',  # unparseable timestamp
        '{"abc": "not-a-dict"}',  # wrong entry shape
        '["not", "a", "dict"]',  # wrong top-level shape
    ],
)
def test_malformed_state_fails_open(tmp_path: Path, payload: str) -> None:
    """Unreadable suppression state re-surfaces items rather than hiding them.

    Failing open is the safe direction here: the cost is a duplicate proposal,
    where failing closed would silently withhold a live alert forever.
    """
    suppress_path(tmp_path).write_text(payload, encoding="utf-8")

    result = active_cooldowns(tmp_path, now=_NOW)

    assert result == set()


@pytest.mark.unit
def test_retired_includes_pending_proposals(tmp_path: Path) -> None:
    """A proposal awaiting review retires its item with no cooldown entry."""
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    (outbox / "2026-06-03-0123456789abcdef-acme-stall.md").write_text("# p", encoding="utf-8")

    result = retired_item_ids(tmp_path, now=_NOW)

    assert result == {"0123456789abcdef"}
    assert not suppress_path(tmp_path).exists()


@pytest.mark.unit
def test_retired_includes_current_but_excludes_invalid_envelope(tmp_path: Path) -> None:
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    (outbox / "current.proposal.json").write_text(
        '{"command_argv":["gtask","complete","task-1","--confirm"],'
        '"created_at":"2026-09-10T12:00:00+00:00","item_id":"fedcba9876543210",'
        '"markdown":"# Proposal\\n","version":1}',
        encoding="utf-8",
    )
    (outbox / "2026-09-10-aaaaaaaaaaaaaaaa-invalid.proposal.json").write_text("{}", encoding="utf-8")

    result = retired_item_ids(tmp_path, now=_NOW)

    assert result == {"fedcba9876543210"}


@pytest.mark.unit
def test_deleting_a_proposal_un_retires_the_item(tmp_path: Path) -> None:
    """Handling a proposal (deleting the file) lets the condition return.

    A condition that survived the operator's intervention should reappear —
    that is the signal the intervention did not take.
    """
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    proposal = outbox / "2026-06-03-0123456789abcdef-acme-stall.md"
    proposal.write_text("# p", encoding="utf-8")
    assert retired_item_ids(tmp_path, now=_NOW) == {"0123456789abcdef"}

    proposal.unlink()

    result = retired_item_ids(tmp_path, now=_NOW)
    assert result == set()
