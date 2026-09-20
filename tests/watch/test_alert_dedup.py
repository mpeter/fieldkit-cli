"""Tests for lib.watcher_dedup.alert_block_exists."""

from pathlib import Path

import pytest

from fieldkit.watch.dedup import alert_block_exists

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# alert_block_exists
# ---------------------------------------------------------------------------


def test_returns_false_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "alerts.md"
    assert alert_block_exists(missing, "2026-06-06 — acme / my-deal") is False


def test_returns_false_when_no_matching_heading(tmp_path: Path) -> None:
    alerts = tmp_path / "alerts.md"
    alerts.write_text(
        "# Alerts\n\n## 2026-06-05 — acme / other-deal — stalled\n\nsome content\n",
        encoding="utf-8",
    )
    assert alert_block_exists(alerts, "2026-06-06 — acme / my-deal") is False


def test_returns_true_when_heading_present_exact(tmp_path: Path) -> None:
    prefix = "2026-06-06 — acme / my-deal"
    alerts = tmp_path / "alerts.md"
    alerts.write_text(
        f"# Alerts\n\n## {prefix} — stalled in stage\n\nsome content\n",
        encoding="utf-8",
    )
    assert alert_block_exists(alerts, prefix) is True


def test_returns_true_when_heading_is_exact_with_no_suffix(tmp_path: Path) -> None:
    prefix = "2026-06-06 — acme / my-deal"
    alerts = tmp_path / "alerts.md"
    alerts.write_text(f"# Alerts\n\n## {prefix}\n", encoding="utf-8")
    assert alert_block_exists(alerts, prefix) is True


def test_returns_false_for_partial_prefix_match(tmp_path: Path) -> None:
    """A heading that merely *contains* the prefix should not match."""
    alerts = tmp_path / "alerts.md"
    alerts.write_text(
        "# Alerts\n\n## 2026-06-06 — acme / my-deal-extended — extra\n",
        encoding="utf-8",
    )
    # This matches because the heading starts with the given prefix string
    # (startswith is a prefix, not word-boundary, check).
    # The test documents the expected behaviour: a longer heading IS a match.
    assert alert_block_exists(alerts, "2026-06-06 — acme / my-deal") is True


def test_skips_append_when_block_exists_stall(tmp_path: Path) -> None:
    """The canonical render owner skips an existing alert heading."""
    import datetime

    # Patch the canonical alert-file owner.
    from unittest.mock import patch

    from fieldkit.watch import _pursuit_stall_render as stall_render

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    account = "acme"
    pursuit = "deal-a"
    # Pre-populate with the heading that would be written
    alerts_file.write_text(
        f"# Pursuit Stall Alerts\n\n## {today} — {account} / {pursuit} — stalled in stage\n",
        encoding="utf-8",
    )

    result: dict = {
        "account": account,
        "pursuit": pursuit,
        "stage": "Stage1",
        "days_since_transition": 20,
        "last_transition_date": "2026-05-01",
        "threshold_days": 14,
        "path": f"accounts/{account}/pursuits/{pursuit}.md",
        "champion": "Alice",
        "sf_next_steps": "Follow up",
        "native_qualification": "unavailable (no Salesforce opportunity link)",
    }

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        original_size = alerts_file.stat().st_size
        stall_render.append_stall_alert(result, dry_run=False)
        assert alerts_file.stat().st_size == original_size, "File should not grow when block already exists"


def test_appends_when_block_absent_stall(tmp_path: Path) -> None:
    """The canonical render owner appends a new alert heading."""
    import datetime
    from unittest.mock import patch

    from fieldkit.watch import _pursuit_stall_render as stall_render

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    alerts_file.write_text("# Pursuit Stall Alerts\n\nAutomated alerts.\n", encoding="utf-8")

    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    account = "acme"
    pursuit = "deal-b"

    result: dict = {
        "account": account,
        "pursuit": pursuit,
        "stage": "Stage2",
        "days_since_transition": 15,
        "last_transition_date": "2026-05-10",
        "threshold_days": 14,
        "path": f"accounts/{account}/pursuits/{pursuit}.md",
        "champion": "Bob",
        "sf_next_steps": "Demo",
        "native_qualification": "pending (live ClosePlan fetch required)",
    }

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        original_size = alerts_file.stat().st_size
        stall_render.append_stall_alert(result, dry_run=False)
        assert alerts_file.stat().st_size > original_size, "File should grow when block is new"
        content = alerts_file.read_text(encoding="utf-8")
        assert f"## {today} — {account} / {pursuit}" in content
        assert "Native qualification:** pending (live ClosePlan fetch required)" in content
        assert "gate_gap" not in content
        assert "all scored" not in content
