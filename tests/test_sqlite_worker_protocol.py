"""Private snapshot workers use typed outcomes, never magic process statuses."""

import pytest

from fieldkit import sqlite_read
from fieldkit.errors import SQLiteSnapshotError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("code", "status"),
    [(0, "ready"), (3, "active"), (3, "journal"), (3, "oversized"), (3, "cleanup"), (3, "invalid")],
)
def test_snapshot_status_accepts_only_consistent_typed_results(code: int, status: str) -> None:
    result = sqlite_read._parse_snapshot_status(code, '{"status": "' + status + '"}\n')

    assert result == status


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (4, '{"status": "active"}'),
        (5, '{"status": "journal"}'),
        (0, '{"status": "invalid"}'),
        (0, '{"status": "oversized"}'),
        (0, '{"status": "cleanup"}'),
        (3, '{"status": "ready"}'),
        (0, ""),
        (0, '{"status": "ready", "status": "ready"}'),
        (0, '{"status": "ready", "detail": "private"}'),
        (0, '{"status": "unknown"}'),
        (0, "[]"),
    ],
)
def test_snapshot_status_rejects_ambiguous_or_unverified_protocol(code: int, stdout: str) -> None:
    with pytest.raises(SQLiteSnapshotError, match="worker result"):
        sqlite_read._parse_snapshot_status(code, stdout)
