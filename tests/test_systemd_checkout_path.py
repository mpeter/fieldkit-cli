"""Contracts for user-service checkout-path defaults."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "config" / "systemd"
OPS_RUNBOOK = ROOT / "docs" / "ops-runbook.md"
SUPPORTED_CHECKOUT = "%h/fieldkit-cli"
CODE_CHECKOUT_UNITS = (
    "fieldkit-companion.service",
    "fieldkit-driver.service",
    "fieldkit-health.service",
    "fieldkit-mother-hen.service",
    "fieldkit-watch.service",
    "fieldkit-web.service",
)


def test_code_checkout_services_use_the_supported_checkout_layout() -> None:
    """Services that invoke checkout code agree on the documented default."""
    if not SYSTEMD_DIR.is_dir():
        pytest.skip("operator-specific systemd units are excluded from the public tree")
    for unit_name in CODE_CHECKOUT_UNITS:
        unit_text = (SYSTEMD_DIR / unit_name).read_text(encoding="utf-8")

        assert f"WorkingDirectory={SUPPORTED_CHECKOUT}" in unit_text
        assert "WorkingDirectory=%h/src/fieldkit-cli" not in unit_text


def test_ops_runbook_documents_the_same_checkout_default() -> None:
    """User-service setup does not instruct operators to use the obsolete path."""
    if not OPS_RUNBOOK.is_file():
        pytest.skip("private operations runbook is excluded from the public tree")
    runbook_text = OPS_RUNBOOK.read_text(encoding="utf-8")

    assert "~/src/fieldkit-cli" not in runbook_text
    assert "~/fieldkit-cli" in runbook_text
