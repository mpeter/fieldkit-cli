"""Tests for _sync_action_items_to_tasks() — covering uncovered branches.

cc=17, cov=71%, target: empty action_items, exception swallowed,
vault_path missing, TASKS.md write, display overrides.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.ingest.writeback import _sync_action_items_to_tasks

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vault_file(tmp_path: Path, content: str = "") -> Path:
    """Create a minimal vault file (meeting note) in tmp_path."""
    f = tmp_path / "meetings" / "2026-06-19-standup.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    if content:
        f.write_text(content, encoding="utf-8")
    else:
        f.write_text(
            "---\nattendees_internal:\n  - Alice\nattendees_external:\n  - Bob\n---\n# Standup\n",
            encoding="utf-8",
        )
    return f


# ---------------------------------------------------------------------------
# Empty action_items → my_only is empty → no TASKS.md write
# ---------------------------------------------------------------------------


# ── TestSyncActionItemsEmpty (flattened) ────────────────────────────────────


def test_sync_action_items_empty_empty_action_items_does_not_write_tasks(tmp_path: Path) -> None:
    """No action items → no TASKS.md write; function returns silently."""
    vault = _vault_file(tmp_path)
    tasks_path = tmp_path / "TASKS.md"

    # _sync_action_items_to_tasks uses lazy imports inside the function body.
    # We patch at the source modules since they're imported with 'from ... import'.
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[]),
        patch("fieldkit.tasks.writer.append_to_tasks"),
    ):
        _sync_action_items_to_tasks(
            action_items=[],
            pursuits=["deal-a"],
            account="acme-corp",
            meeting_date="2026-06-19",
            meeting_title="Standup",
            data_root=tmp_path,
            vault_path=vault,
        )

    assert not tasks_path.exists()


# ---------------------------------------------------------------------------
# Exception swallowed silently
# ---------------------------------------------------------------------------


# ── TestSyncActionItemsExceptionSwallowed (flattened) ───────────────────────


def test_sync_action_items_exception_swallowed_exception_does_not_propagate(tmp_path: Path) -> None:
    """Any exception during sync is caught and logged as a warning, not re-raised."""
    vault = _vault_file(tmp_path)

    # Patch _get_user_name to raise an unexpected error
    with patch("fieldkit.config.get_user_name", side_effect=RuntimeError("config unavailable")):
        notices = _sync_action_items_to_tasks(
            action_items=["Follow up with champion"],
            pursuits=["deal-a"],
            account="acme-corp",
            meeting_date="2026-06-19",
            meeting_title="Standup",
            data_root=tmp_path,
            vault_path=vault,
        )

    assert len(notices) == 1
    assert notices[0].err is True
    assert notices[0].message == "  Warning: action item sync failed: config unavailable"


# ---------------------------------------------------------------------------
# vault_path missing → vault frontmatter read skipped
# ---------------------------------------------------------------------------


# ── TestSyncActionItemsVaultMissing (flattened) ─────────────────────────────


def test_sync_action_items_vault_missing_missing_vault_path_does_not_raise(tmp_path: Path) -> None:
    """When vault_path does not exist, frontmatter read is skipped silently."""
    missing_vault = tmp_path / "meetings" / "nonexistent.md"

    mock_classified = [MagicMock(cls=MagicMock(value="my_task"))]
    mock_classified[0].cls.value = "my_task"

    tasks_path = tmp_path / "TASKS.md"
    tasks_path.write_text("# Tasks\n\n## Today\n\n", encoding="utf-8")

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=mock_classified),
        patch("fieldkit.tasks.writer.append_to_tasks", return_value=(1, [])),
    ):
        # Should not raise even though vault_path is missing
        notices = _sync_action_items_to_tasks(
            action_items=["Prepare proposal"],
            pursuits=["deal-b"],
            account="acme-corp",
            meeting_date="2026-06-19",
            meeting_title="Deal Sync",
            data_root=tmp_path,
            vault_path=missing_vault,
        )

    assert len(notices) == 1
    assert notices[0].err is False
    assert notices[0].message.startswith("  Tasks: +1 active → TASKS.md")


# ---------------------------------------------------------------------------
# Display overrides applied to pursuit label
# ---------------------------------------------------------------------------


# ── TestSyncActionItemsDisplayOverrides (flattened) ─────────────────────────


def _sync_action_items_display_overrides_run_with_capture(
    tmp_path: Path,
    pursuits: list[str],
    account: str,
    action_items: list[str],
) -> list[str]:
    """Run _sync_action_items_to_tasks, return captured pursuit_label calls."""
    vault = _vault_file(tmp_path)
    captured_labels: list[str] = []

    def capture_classify(items, *, pursuit_label, **kwargs):
        captured_labels.append(pursuit_label)
        return []

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.tasks.classifier.classify_action_items", side_effect=capture_classify),
    ):
        _sync_action_items_to_tasks(
            action_items=action_items,
            pursuits=pursuits,
            account=account,
            meeting_date="2026-06-19",
            meeting_title="Sync",
            data_root=tmp_path,
            vault_path=vault,
        )
    return captured_labels


def test_sync_action_items_display_overrides_known_abbreviation_ocp_override(tmp_path: Path) -> None:
    """Known abbreviation like 'ocp' → 'OCP' in the pursuit label."""
    labels = _sync_action_items_display_overrides_run_with_capture(
        tmp_path, ["ocp-migration"], "acme-corp", ["Test action"]
    )
    assert len(labels) == 1
    assert "OCP" in labels[0], f"Expected 'OCP' in label, got: {labels[0]!r}"


def test_sync_action_items_display_overrides_no_pursuits_uses_account_only(tmp_path: Path) -> None:
    """With no pursuits list, label uses account display name only."""
    labels = _sync_action_items_display_overrides_run_with_capture(tmp_path, [], "acme-corp", ["Prepare proposal"])
    assert len(labels) == 1
    label = labels[0]
    assert "Acme" in label or "acme" in label.lower()


def test_sync_action_items_display_overrides_aap_abbreviation_override(tmp_path: Path) -> None:
    """'aap' → 'AAP' in pursuit label."""
    labels = _sync_action_items_display_overrides_run_with_capture(
        tmp_path, ["aap-upgrade"], "acme-corp", ["Test action"]
    )
    if labels:
        assert "AAP" in labels[0]


# ── TestSyncActionItemsWaitingOnNotAutoWritten (flattened) ──────────────────


def test_sync_action_items_waiting_on_not_auto_written_waiting_on_items_not_written_to_tasks(tmp_path: Path) -> None:
    """WAITING_ON classified items must NOT be auto-written to TASKS.md."""
    vault = _vault_file(tmp_path)
    tasks_path = tmp_path / "TASKS.md"
    tasks_path.write_text("# Tasks\n\n## Today\n\n", encoding="utf-8")

    # Only WAITING_ON classified items (no MY_TASK)
    waiting_item = MagicMock()
    waiting_item.cls.value = "waiting_on"

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[waiting_item]),
        patch("fieldkit.tasks.writer.append_to_tasks") as mock_append,
    ):
        _sync_action_items_to_tasks(
            action_items=["Waiting for legal review"],
            pursuits=["deal-c"],
            account="globalpay",
            meeting_date="2026-06-19",
            meeting_title="Deal Sync",
            data_root=tmp_path,
            vault_path=vault,
        )

    # append_to_tasks must NOT be called for WAITING_ON-only items
    mock_append.assert_not_called()
