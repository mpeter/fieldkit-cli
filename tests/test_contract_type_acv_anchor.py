"""Consumer re-anchor tests for historic regression / #1206 — fixed-price ACV correction.

Locks that forecast, quota, and account roll-ups anchor on the net ACV
(sf_acv / acv) for fixed-price consulting opps (where the gross consulting
total runs ~2x net), while leaving standard (T&M / prepaid-credit) opps on the
gross consulting total. These lock corrected behavior — not characterization.
"""

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.pipeline.quota import _collect_pursuits_for_quota
from fieldkit.commands.pursuit.forecast import compute_forecast
from fieldkit.commands.sf.account import (
    _build_account_write_payload,
    _enrich_contract_type,
)
from fieldkit.sf.client import SFAPIError, SFAuthError

pytestmark = pytest.mark.unit

_TODAY = date(2026, 6, 1)
_GROSS = 1_000_000.0  # gross consulting total (Consulting_Total_USD__c)
_NET = 500_000.0  # net ACV (ACV_Opportunity_USD__c) ~= half the gross


def _pursuit_md(*, contract_type: str | None) -> str:
    ct_line = f"sf_contract_type: {contract_type}\n" if contract_type is not None else ""
    return (
        "---\n"
        "stage: negotiate\n"
        f"sf_consulting_acv: {_GROSS}\n"
        f"sf_acv: {_NET}\n"
        "sf_arr: 750000\n"
        f"{ct_line}"
        "---\n\n# Pursuit\n"
    )


def _write_pursuit(tmp_path: Path, *, contract_type: str | None) -> Path:
    p = tmp_path / "accounts" / "acme-corp" / "pursuits"
    p.mkdir(parents=True, exist_ok=True)
    (p / "deal.md").write_text(_pursuit_md(contract_type=contract_type), encoding="utf-8")
    return tmp_path


# ── forecast ──────────────────────────────────────────────────────────────────


def test_forecast_fixed_price_anchors_on_net_acv(tmp_path: Path) -> None:
    root = _write_pursuit(tmp_path, contract_type="fixed_price")
    result = compute_forecast(root, today=_TODAY)
    assert result.deals[0].acv == pytest.approx(_NET)
    assert result.commit == pytest.approx(_NET)


def test_forecast_standard_unchanged_uses_gross_consulting(tmp_path: Path) -> None:
    root = _write_pursuit(tmp_path, contract_type="standard")
    result = compute_forecast(root, today=_TODAY)
    assert result.deals[0].acv == pytest.approx(_GROSS)


def test_forecast_absent_contract_type_uses_gross_consulting(tmp_path: Path) -> None:
    """No sf_contract_type (unsynced file) is treated as standard — backward compatible."""
    root = _write_pursuit(tmp_path, contract_type=None)
    result = compute_forecast(root, today=_TODAY)
    assert result.deals[0].acv == pytest.approx(_GROSS)


# ── quota ───────────────────────────────────────────────────────────────────


def _quota_fm(*, contract_type: str) -> MagicMock:
    fm = MagicMock()
    fm.stage = "negotiate"
    fm.sf_contract_type = contract_type
    fm.sf_consulting_acv = _GROSS
    fm.sf_acv = _NET
    fm.sf_arr = 750_000.0
    fm.sf_probability = None
    return fm


def _collect_one(fm: MagicMock) -> list[dict[str, object]]:
    with (
        patch("fieldkit.commands.pipeline.quota.iterate_pursuits", return_value=[Path("/fake/deal.md")]),
        patch("fieldkit.commands.pipeline.quota.load_pursuit", return_value=(fm, "", 0.0)),
    ):
        return _collect_pursuits_for_quota(Path("/fake/data"))


def test_quota_fixed_price_anchors_on_net_acv() -> None:
    collected = _collect_one(_quota_fm(contract_type="fixed_price"))
    assert collected[0]["sf_amount"] == pytest.approx(_NET)


def test_quota_standard_unchanged_uses_gross_consulting() -> None:
    collected = _collect_one(_quota_fm(contract_type="standard"))
    assert collected[0]["sf_amount"] == pytest.approx(_GROSS)


# ── account ───────────────────────────────────────────────────────────────────


def test_account_payload_fixed_price_rolls_up_net() -> None:
    opps = [{"contract_type": "fixed_price", "acv": _NET, "consulting_acv": _GROSS, "training_acv": 0.0}]
    payload = _build_account_write_payload("acme-corp", None, opps)
    assert payload["open_consulting_acv"] == pytest.approx(_NET)


def test_account_payload_standard_rolls_up_gross() -> None:
    opps = [{"contract_type": "standard", "acv": _NET, "consulting_acv": _GROSS, "training_acv": 0.0}]
    payload = _build_account_write_payload("acme-corp", None, opps)
    assert payload["open_consulting_acv"] == pytest.approx(_GROSS)


def test_enrich_contract_type_walk_failure_degrades_to_standard() -> None:
    """A non-auth walk failure leaves the opp as standard so the render continues."""
    opp: dict[str, object] = {"opportunity_id": "006000000000000AAA", "consulting_acv": _GROSS}
    client = MagicMock()
    client.fetch_related_list_records.side_effect = SFAPIError("boom")
    _enrich_contract_type(opp, client)
    assert opp["contract_type"] == "standard"


def test_enrich_contract_type_fixed_price_fetches_net_acv() -> None:
    opp: dict[str, object] = {"opportunity_id": "006000000000000AAA", "consulting_acv": _GROSS, "acv": None}
    client = MagicMock()
    client.fetch_related_list_records.side_effect = [
        [{"id": "a0Q000000000000AAA"}],  # opp -> quotes
        [{"id": "a0R000000000000AAA", "fields": {"SBQQ__ProductFamily__c": {"value": "CONSULTING - FIXED PRICE"}}}],
    ]
    client.fetch_record.return_value = {"ACV_Opportunity_USD__c": _NET}
    _enrich_contract_type(opp, client)
    assert opp["contract_type"] == "fixed_price"
    assert opp["acv"] == pytest.approx(_NET)


def test_enrich_contract_type_auth_error_propagates() -> None:
    opp: dict[str, object] = {"opportunity_id": "006000000000000AAA"}
    client = MagicMock()
    client.fetch_related_list_records.side_effect = SFAuthError("expired")
    with pytest.raises(SFAuthError, match=r"expired"):
        _enrich_contract_type(opp, client)
