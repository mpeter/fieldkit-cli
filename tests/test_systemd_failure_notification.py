"""Contract tests for the local Mother-Hen failure notification units."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MOTHER_HEN_UNIT = ROOT / "config" / "systemd" / "fieldkit-mother-hen.service"
NOTIFIER_UNIT = ROOT / "config" / "systemd" / "fieldkit-failure-notify@.service"
OPS_RUNBOOK = ROOT / "docs" / "ops-runbook.md"


def test_mother_hen_failure_starts_templated_notifier() -> None:
    if not MOTHER_HEN_UNIT.is_file():
        pytest.skip("operator-specific systemd units are excluded from the public tree")
    unit_text = MOTHER_HEN_UNIT.read_text(encoding="utf-8")

    assert "OnFailure=fieldkit-failure-notify@%n.service" in unit_text


def test_failure_notifier_is_local_critical_notification_with_recovery_command() -> None:
    if not NOTIFIER_UNIT.is_file():
        pytest.skip("operator-specific systemd units are excluded from the public tree")
    unit_text = NOTIFIER_UNIT.read_text(encoding="utf-8")

    assert "Description=fieldkit failure notification for %i" in unit_text
    assert (
        'ExecStart=/usr/bin/notify-send --urgency=critical "fieldkit failure: %i" '
        '"Recover: journalctl --user -u %i -n 50"'
    ) in unit_text
    assert "/bin/sh" not in unit_text


def test_mother_hen_installation_includes_failure_notifier() -> None:
    if not OPS_RUNBOOK.is_file():
        pytest.skip("private operations runbook is excluded from the public tree")
    runbook_text = OPS_RUNBOOK.read_text(encoding="utf-8")

    assert "cp config/systemd/fieldkit-failure-notify@.service ~/.config/systemd/user/" in runbook_text
