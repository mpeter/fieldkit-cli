"""Unit tests for --limit-per-account behavior in _scan_all_accounts.

Spec 032 — implementation note: _scan_all_accounts computes an effective_limit as
min(limit, limit_per_account) when limit_per_account is set, and passes
it as search_limit to scan_account_threads.

All tests patch scan_account_threads and the alert/state side-effects to
keep tests fast and filesystem-free.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.watch.slack_threads import _scan_all_accounts

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW_UTC = datetime.datetime(2026, 6, 25, 12, 0, 0, tzinfo=datetime.UTC)
_RUN_TS = "2026-06-25T12:00:00Z"

# Minimal account dict that passes all guards in _scan_all_accounts:
#   - isinstance(account_cfg, dict) ✓
#   - slack_watch is not False ✓
#   - internal is not set ✓
_ACCOUNTS: dict = {"acme": {"keywords": ["acme"], "slack_watch": True}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_scan(
    *,
    limit: int,
    limit_per_account: int | None,
    mock_scan: MagicMock,
) -> None:
    """Invoke _scan_all_accounts with standard args, patching all side-effects."""
    with (
        patch(
            "fieldkit.watch.slack_threads.scan_account_threads",
            mock_scan,
        ),
        patch("fieldkit.watch.slack_threads.append_thread_alert"),
        patch("fieldkit.watch.slack_threads.write_auth_error_alert"),
        patch("fieldkit.watch.slack_threads._save_auth_error_state"),
    ):
        _scan_all_accounts(
            _ACCOUNTS,
            account_filter=None,
            current_username=None,
            threshold_hours=48,
            limit=limit,
            limit_per_account=limit_per_account,
            now_utc=_NOW_UTC,
            run_ts=_RUN_TS,
            updated_state={},
            previous_state={},
            dry_run=True,
            config={},
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_limit_per_account_applied() -> None:
    """When limit=50 and limit_per_account=5, scan_account_threads receives search_limit=5."""
    mock_scan = MagicMock(return_value=[])

    _call_scan(limit=50, limit_per_account=5, mock_scan=mock_scan)

    mock_scan.assert_called_once()
    _, kwargs = mock_scan.call_args
    assert kwargs["search_limit"] == 5, f"Expected search_limit=5 (min(50, 5)), got {kwargs['search_limit']}"


def test_global_limit_wins_when_smaller() -> None:
    """When limit=3 and limit_per_account=10, scan_account_threads receives search_limit=3."""
    mock_scan = MagicMock(return_value=[])

    _call_scan(limit=3, limit_per_account=10, mock_scan=mock_scan)

    mock_scan.assert_called_once()
    _, kwargs = mock_scan.call_args
    assert kwargs["search_limit"] == 3, f"Expected search_limit=3 (min(3, 10)), got {kwargs['search_limit']}"


def test_no_limit_per_account_uses_global() -> None:
    """When limit_per_account=None, scan_account_threads receives search_limit=limit."""
    mock_scan = MagicMock(return_value=[])

    _call_scan(limit=50, limit_per_account=None, mock_scan=mock_scan)

    mock_scan.assert_called_once()
    _, kwargs = mock_scan.call_args
    assert kwargs["search_limit"] == 50, f"Expected search_limit=50 (no per-account cap), got {kwargs['search_limit']}"
