"""Tests for fieldkit.pursuit.pipeline_health — risk classification."""

from datetime import date
from pathlib import Path

import pytest

from fieldkit.commands.pursuit.audit import audit_file
from fieldkit.commands.pursuit.pipeline_health import RiskItem, classify_pursuit, health_check

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_FM = """\
---
stage: discover
gate-status: pending
last-transition: 2026-05-10
transition-history: []
meddpicc:
  metrics: 2
  economic-buyer: 2
  decision-criteria: 2
  decision-process: 2
  identify-pain: 2
  champion: 2
  competition: 2
  paper-process: 2
sf_opportunity_id: "006Pe000012n2GkIAI"
sf_stage: Discover
sf_close_date: 12/31/2027
sf_arr: $100,000.00
sf_owner: Jane Doe
sf_next_steps: next step
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit
"""

TODAY = date(2026, 6, 1)


def _make_pursuit(tmp_path: Path, content: str, name: str = "deal.md") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _audit_and_classify(path: Path, today: date = TODAY) -> RiskItem | None:
    result = audit_file(path, today=today)
    return classify_pursuit(result, today)


# ---------------------------------------------------------------------------
# classify_pursuit — closed stages skipped
# ---------------------------------------------------------------------------


def test_classify_skips_pursuit_removed_after_audit(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, BASE_FM)
    audited = audit_file(path, today=TODAY)
    path.unlink()

    result = classify_pursuit(audited, TODAY)

    assert result is None


def test_classify_skips_invalid_frontmatter_after_audit(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, BASE_FM)
    audited = audit_file(path, today=TODAY)
    path.write_text("No frontmatter here.\n", encoding="utf-8")

    result = classify_pursuit(audited, TODAY)

    assert result is None


def test_classify_skips_closed_won(tmp_path: Path) -> None:
    content = BASE_FM.replace("stage: discover", "stage: closed-won")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is None


def test_classify_skips_closed_lost(tmp_path: Path) -> None:
    content = BASE_FM.replace("stage: discover", "stage: closed-lost")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is None


def test_classify_skips_pre_pipeline(tmp_path: Path) -> None:
    content = BASE_FM.replace("stage: discover", "stage: pre-pipeline")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is None


# ---------------------------------------------------------------------------
# classify_pursuit — LOW risk (healthy deal)
# ---------------------------------------------------------------------------


def test_classify_low_risk_healthy(tmp_path: Path) -> None:
    p = _make_pursuit(tmp_path, BASE_FM)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "LOW"
    assert item.risk_reasons == []


# ---------------------------------------------------------------------------
# classify_pursuit — HIGH risk
# ---------------------------------------------------------------------------


def test_classify_high_overdue_close_date(tmp_path: Path) -> None:
    content = BASE_FM.replace("sf_close_date: 12/31/2027", "sf_close_date: 1/1/2025")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "HIGH"
    assert any("Overdue" in r for r in item.risk_reasons)


def test_classify_ignores_legacy_champion_and_economic_buyer(tmp_path: Path) -> None:
    content = BASE_FM.replace("  champion: 2", "  champion: 0").replace("  economic-buyer: 2", "  economic-buyer: 1")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "LOW"
    assert item.qualification_status == "unavailable"
    assert not any("champion" in r.lower() for r in item.risk_reasons)


def test_classify_ignores_legacy_zero_score(tmp_path: Path) -> None:
    content = BASE_FM
    for elem in [
        "metrics",
        "economic-buyer",
        "decision-criteria",
        "decision-process",
        "identify-pain",
        "champion",
        "competition",
        "paper-process",
    ]:
        content = content.replace(f"  {elem}: 2", f"  {elem}: 0")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "LOW"
    assert item.qualification_status == "unavailable"
    assert not any("score" in reason.lower() for reason in item.risk_reasons)


# ---------------------------------------------------------------------------
# classify_pursuit — MEDIUM risk
# ---------------------------------------------------------------------------


def test_classify_medium_approaching_close_wrong_stage(tmp_path: Path) -> None:
    # Close in 15 days, but stage=discover (not propose/negotiate)
    content = BASE_FM.replace("sf_close_date: 12/31/2027", "sf_close_date: 6/16/2026")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p, today=TODAY)
    assert item is not None
    assert item.risk_tier == "MEDIUM"
    assert any("stage" in r.lower() for r in item.risk_reasons)


def test_classify_legacy_values_do_not_create_threshold_risk(tmp_path: Path) -> None:
    content = BASE_FM
    for elem in [
        "metrics",
        "economic-buyer",
        "decision-criteria",
        "decision-process",
        "identify-pain",
        "champion",
        "competition",
        "paper-process",
    ]:
        content = content.replace(f"  {elem}: 2", f"  {elem}: 1")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "LOW"
    assert item.qualification_status == "unavailable"
    assert not any("qualification" in r.lower() for r in item.risk_reasons)


def test_classify_medium_missing_sf_id(tmp_path: Path) -> None:
    content = BASE_FM.replace('sf_opportunity_id: "006Pe000012n2GkIAI"', "sf_opportunity_id: ")
    p = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(p)
    assert item is not None
    assert item.risk_tier == "MEDIUM"
    assert any("sf_opportunity_id" in r for r in item.risk_reasons)


@pytest.mark.parametrize(
    ("close_date", "days_until"),
    [("6/14/2026", 13), ("6/15/2026", 14)],
)
def test_classify_medium_negotiate_close_soon_low_score_boundary(
    tmp_path: Path, close_date: str, days_until: int
) -> None:
    content = BASE_FM.replace("stage: discover", "stage: negotiate").replace(
        "sf_close_date: 12/31/2027", f"sf_close_date: {close_date}"
    )
    content = content.replace("  metrics: 2", "  metrics: 1")
    pursuit = _make_pursuit(tmp_path, content)
    item = _audit_and_classify(pursuit, today=TODAY)

    assert isinstance(item, RiskItem)
    assert item.days_until_close == days_until
    assert item.qualification_status == "unavailable"
    assert item.risk_tier == "LOW"
    assert not any("score" in reason.lower() for reason in item.risk_reasons)


def test_classify_negotiate_close_soon_does_not_use_legacy_score(tmp_path: Path) -> None:
    content = BASE_FM.replace("stage: discover", "stage: negotiate").replace(
        "sf_close_date: 12/31/2027", "sf_close_date: 6/9/2026"
    )
    for element in [
        "metrics",
        "economic-buyer",
        "decision-criteria",
        "decision-process",
        "identify-pain",
        "champion",
        "competition",
        "paper-process",
    ]:
        content = content.replace(f"  {element}: 2", f"  {element}: 1")
    content = content.replace("  metrics: 1", "  metrics: 0")
    item = _audit_and_classify(_make_pursuit(tmp_path, content), today=TODAY)

    assert item is not None
    assert item.qualification_status == "unavailable"
    assert item.risk_tier == "LOW"
    assert item.risk_reasons == []


# ---------------------------------------------------------------------------
# health_check — directory scan
# ---------------------------------------------------------------------------


def test_health_check_scans_directory(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "deal.md").write_text(BASE_FM, encoding="utf-8")

    items = health_check(tmp_path, today=TODAY)
    assert len(items) == 1
    assert items[0].risk_tier == "LOW"


def test_health_check_excludes_closed(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "open.md").write_text(BASE_FM, encoding="utf-8")
    closed = BASE_FM.replace("stage: discover", "stage: closed-won")
    (accounts / "closed.md").write_text(closed, encoding="utf-8")

    items = health_check(tmp_path, today=TODAY)
    assert len(items) == 1
    assert "open" in items[0].relative_path


def test_health_check_sorted_high_first(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    # Healthy deal
    (accounts / "low.md").write_text(BASE_FM, encoding="utf-8")
    # Overdue = HIGH
    overdue = BASE_FM.replace("sf_close_date: 12/31/2027", "sf_close_date: 1/1/2025")
    (accounts / "high.md").write_text(overdue, encoding="utf-8")

    items = health_check(tmp_path, today=TODAY)
    assert items[0].risk_tier == "HIGH"
    assert items[-1].risk_tier == "LOW"


def test_health_check_account_filter(tmp_path: Path) -> None:
    for account in ("acme", "globex"):
        p = tmp_path / "accounts" / account / "pursuits"
        p.mkdir(parents=True)
        (p / "deal.md").write_text(BASE_FM, encoding="utf-8")

    items = health_check(tmp_path, account_filter="acme", today=TODAY)
    assert len(items) == 1
    assert "acme" in items[0].relative_path


def test_health_check_empty(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    items = health_check(tmp_path, today=TODAY)
    assert items == []
