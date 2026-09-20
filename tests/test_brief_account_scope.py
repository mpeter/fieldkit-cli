"""Tests for historic regression: --account scoping must cover the Project Health section too.

Regression coverage for the leak where ``fieldkit brief generate --account X``
scoped pipeline review to one account but the Project Health section in the same
brief still aggregated zombie/expiring project counts across ALL accounts.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_render import (
    _render_project_health_section,
    render_brief,
)

pytestmark = pytest.mark.unit


def _write_project(accounts_dir: Path, account: str, sf_contract_end: date, sf_stage: str = "Negotiation") -> None:
    projects_dir = accounts_dir / account / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    (projects_dir / "deal.md").write_text(
        f'---\nsf_stage: {sf_stage}\nsf_contract_end: "{sf_contract_end.isoformat()}"\n'
        f"sf_opportunity: {account} Deal\n---\nProject notes.\n",
        encoding="utf-8",
    )


def _two_account_fixture(tmp_path: Path) -> Path:
    """account-alpha has a ZOMBIE project (contract ended, stage not completed);
    account-beta has an EXPIRING project (contract ends within 30 days)."""
    accounts_dir = tmp_path / "accounts"
    today = datetime.now(tz=UTC).date()
    _write_project(accounts_dir, "account-alpha", today - timedelta(days=10))
    _write_project(accounts_dir, "account-beta", today + timedelta(days=15))
    return tmp_path


def _minimal_brief_kwargs() -> dict[str, Any]:
    return {
        "target_date": datetime.now(tz=UTC).date(),
        "meetings": [],
        "backstory_alerts": [],
        "pursuit_stall_alerts": [],
        "slack_alerts": [],
        "pipeline_review_md": "",
        "elapsed_seconds": 0.0,
        "quota_collector": lambda _root: [],
        "cross_account_signals": [],
    }


def test_render_project_health_section_scopes_to_account(tmp_path: Path) -> None:
    data_root = _two_account_fixture(tmp_path)

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=data_root):
        result = _render_project_health_section(account="account-alpha")

    assert result
    summary = " ".join(result)
    assert "ZOMBIE" in summary
    assert "EXPIRING" not in summary


def test_render_project_health_section_no_account_reports_all(tmp_path: Path) -> None:
    """Regression guard: account=None (default) preserves the pre-fix all-accounts behavior."""
    data_root = _two_account_fixture(tmp_path)

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=data_root):
        result = _render_project_health_section()

    assert result
    summary = " ".join(result)
    assert "ZOMBIE" in summary
    assert "EXPIRING" in summary


def test_render_project_health_section_account_not_found(tmp_path: Path) -> None:
    """F1 (proctor follow-up): an account slug that matches no project directory must
    render an explicit notice, not silently vanish (indistinguishable from a clean
    account otherwise)."""
    data_root = _two_account_fixture(tmp_path)

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=data_root):
        result = _render_project_health_section(account="account-typo")

    assert result
    summary = " ".join(result)
    assert "## Project Health" in summary
    assert "No projects found" in summary
    assert "account-typo" in summary
    assert "ZOMBIE" not in summary
    assert "EXPIRING" not in summary


def test_render_brief_project_health_section_scoped_to_account(tmp_path: Path) -> None:
    data_root = _two_account_fixture(tmp_path)
    kwargs = _minimal_brief_kwargs()
    kwargs["account"] = "account-alpha"

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=data_root):
        output = render_brief(**kwargs)

    assert "## Project Health" in output
    section = output.split("## Project Health", 1)[1]
    assert "ZOMBIE" in section
    assert "EXPIRING" not in section


def test_render_brief_cross_account_section_unaffected_by_account(tmp_path: Path) -> None:
    """Cross-account signal detection is deliberately cross-account by design —
    the `account` scoping param must never filter it."""
    data_root = _two_account_fixture(tmp_path)
    kwargs = _minimal_brief_kwargs()
    kwargs["account"] = "account-alpha"
    kwargs["cross_account_signals"] = [{"topic": "kubernetes", "accounts": ["account-alpha", "account-beta"]}]

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=data_root):
        output = render_brief(**kwargs)

    assert output
    assert "## Cross-Account Signals" in output
    assert "account-alpha, account-beta" in output
