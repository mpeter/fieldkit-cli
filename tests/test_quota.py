"""Tests for calculate_quota_gap arithmetic and edge cases."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.pipeline.quota import _collect_pursuits_for_quota
from fieldkit.watch.morning_brief_render import calculate_quota_gap

# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

QUOTA_100K: dict[str, object] = {"target": 100_000}


def _pursuit(stage: str, amount: object) -> dict[str, object]:
    return {"stage": stage, "sf_amount": amount}


# ---------------------------------------------------------------------------
# Happy-path arithmetic with known pursuits
# ---------------------------------------------------------------------------


# ── TestCalculateQuotaGap (flattened) ───────────────────────────────────────


def test_calculate_quota_gap_three_pursuits_known_amounts() -> None:
    """Three pursuits: closed-won 30k, negotiate 20k (0.75), propose 10k (0.50).

    historic regression: weights now match pursuit forecast (conservative values).
    """
    pursuits = [
        _pursuit("closed-won", 30_000),
        _pursuit("negotiate", 20_000),
        _pursuit("propose", 10_000),
    ]
    result = calculate_quota_gap(pursuits, QUOTA_100K)

    assert result["target"] == pytest.approx(100_000)
    assert result["closed_won"] == pytest.approx(30_000)
    # 20_000 * 0.75 + 10_000 * 0.50 = 15_000 + 5_000 = 20_000
    assert result["weighted"] == pytest.approx(20_000)
    # gap = 100_000 - 30_000 - 20_000 = 50_000
    assert result["gap"] == pytest.approx(50_000)


def test_calculate_quota_gap_all_stage_weights_applied() -> None:
    """Each weighted stage contributes correctly.

    historic regression: weights now match pursuit forecast (conservative values).
    """
    pursuits = [
        _pursuit("negotiate", 10_000),  # 0.75 → 7_500
        _pursuit("propose", 10_000),  # 0.50 → 5_000
        _pursuit("validate", 10_000),  # 0.25 → 2_500
        _pursuit("discover", 10_000),  # 0.10 → 1_000
        _pursuit("qualify", 10_000),  # 0.05 → 500
        _pursuit("prospect", 10_000),  # 0.05 → 500
        _pursuit("pre-pipeline", 10_000),  # 0.00 → 0
    ]
    result = calculate_quota_gap(pursuits, {"target": 0})
    assert result["weighted"] == pytest.approx(17_000)


def test_calculate_quota_gap_dollar_string_amounts() -> None:
    """Dollar-prefixed strings are parsed correctly."""
    pursuits = [
        _pursuit("closed-won", "$50,000"),
        _pursuit("propose", "$20,000"),
    ]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["closed_won"] == pytest.approx(50_000)
    assert result["weighted"] == pytest.approx(10_000)  # 20_000 * 0.50


def test_calculate_quota_gap_gap_negative_when_over_quota() -> None:
    """Gap is negative when closed-won already exceeds quota."""
    pursuits = [_pursuit("closed-won", 200_000)]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["gap"] == pytest.approx(-100_000)


# ---------------------------------------------------------------------------
# Edge cases: missing / bad sf_amount
# ---------------------------------------------------------------------------


# ── TestMissingAmount (flattened) ───────────────────────────────────────────


def test_missing_amount_none_sf_amount_skipped() -> None:
    """Pursuits with sf_amount=None are silently skipped."""
    pursuits = [
        _pursuit("closed-won", None),
        _pursuit("negotiate", 10_000),
    ]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["closed_won"] == pytest.approx(0)
    assert result["weighted"] == pytest.approx(7_500)  # 10_000 * 0.75


def test_missing_amount_unparseable_sf_amount_skipped() -> None:
    """Non-numeric strings like 'TBD' are skipped."""
    pursuits = [
        _pursuit("propose", "TBD"),
        _pursuit("closed-won", 10_000),
    ]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["closed_won"] == pytest.approx(10_000)
    assert result["weighted"] == pytest.approx(0)


def test_missing_amount_empty_string_amount_skipped() -> None:
    pursuits = [_pursuit("validate", "")]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["weighted"] == pytest.approx(0)


def test_missing_amount_missing_sf_amount_key() -> None:
    """Pursuit dict without sf_amount key is treated as None."""
    pursuits: list[dict[str, object]] = [{"stage": "closed-won"}]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["closed_won"] == pytest.approx(0)


# ---------------------------------------------------------------------------
# Edge cases: empty pursuits, no quota configured
# ---------------------------------------------------------------------------


# ── TestEmptyAndZero (flattened) ────────────────────────────────────────────


def test_empty_and_zero_empty_pursuits() -> None:
    result = calculate_quota_gap([], QUOTA_100K)
    assert result["closed_won"] == pytest.approx(0)
    assert result["weighted"] == pytest.approx(0)
    assert result["gap"] == pytest.approx(100_000)


def test_empty_and_zero_no_quota_configured() -> None:
    """target=0 / missing target → gap equals negative of closed+weighted."""
    pursuits = [_pursuit("closed-won", 50_000)]
    result = calculate_quota_gap(pursuits, {})
    assert result["target"] == pytest.approx(0)
    assert result["gap"] == pytest.approx(-50_000)


def test_empty_and_zero_unknown_stage_ignored() -> None:
    """Pursuits in unknown stages (not closed-won, not in weights) are skipped."""
    pursuits = [
        _pursuit("closed-lost", 100_000),
        _pursuit("won-lost", 50_000),
        _pursuit("some-future-stage", 25_000),
    ]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    assert result["closed_won"] == pytest.approx(0)
    assert result["weighted"] == pytest.approx(0)


# ---------------------------------------------------------------------------
# Falsy-zero edge case: sf_consulting_acv=0.0 must not fall through to sf_acv
# ---------------------------------------------------------------------------


# ── TestConsultingAcvZero (flattened) ───────────────────────────────────────


def test_consulting_acv_zero_sf_consulting_acv_zero_not_fallthrough() -> None:
    """sf_amount=0.0 passed directly → weighted is 0.0, not skipped."""
    pursuits = [_pursuit("negotiate", 0.0)]
    result = calculate_quota_gap(pursuits, QUOTA_100K)
    # 0.0 * 0.9 = 0.0, not a skip or fallthrough to another field
    assert result["weighted"] == pytest.approx(0.0)


def test_consulting_acv_zero_collect_pursuits_consulting_acv_zero_not_fallthrough() -> None:
    """_collect_pursuits_for_quota: sf_consulting_acv=0.0 keeps sf_amount=0.0.

    Previously the `or`-chain would fall through to sf_acv=50_000 when
    sf_consulting_acv was 0.0 (falsy).  The fixed is-not-None check must
    stop at sf_consulting_acv and record 0.0.
    """
    fake_fm = MagicMock()
    fake_fm.stage = "negotiate"
    fake_fm.sf_consulting_acv = 0.0  # falsy but explicitly set
    fake_fm.sf_acv = 50_000  # should NOT be used
    fake_fm.sf_arr = 75_000  # should NOT be used
    fake_fm.sf_probability = None

    fake_path = Path("/fake/pursuit.md")

    with (
        patch(
            "fieldkit.pipeline.quota.iterate_pursuits",
            return_value=[fake_path],
        ),
        patch(
            "fieldkit.pipeline.quota.load_pursuit",
            return_value=(fake_fm, "", 0.0),
        ),
    ):
        collected = _collect_pursuits_for_quota(Path("/fake/data"))

    assert len(collected) == 1
    assert collected[0]["sf_amount"] == pytest.approx(0.0)
    # Must NOT have fallen through to sf_acv=50_000
    assert collected[0]["sf_amount"] != pytest.approx(50_000)


# ---------------------------------------------------------------------------
# 4F.1 cmd_quota --set (pipeline quota consolidation, D1 Wave 4)
# ---------------------------------------------------------------------------


# ── TestQuotaCmd (flattened) ────────────────────────────────────────────────


@pytest.mark.unit
def test_quota_cmd_set_invalid_period_exits_nonzero() -> None:
    """cmd_quota --set exits non-zero for malformed period string."""
    from click.testing import CliRunner

    from fieldkit.commands.pipeline.cli import cmd_quota

    result = CliRunner().invoke(cmd_quota, ["--set", "100000", "--period", "invalid-period"])
    assert result.exit_code != 0


@pytest.mark.unit
def test_quota_cmd_set_without_period_exits_nonzero() -> None:
    """cmd_quota --set exits non-zero when --period is missing."""
    from click.testing import CliRunner

    from fieldkit.commands.pipeline.cli import cmd_quota

    result = CliRunner().invoke(cmd_quota, ["--set", "100000"])
    assert result.exit_code != 0


@pytest.mark.unit
def test_quota_cmd_set_valid_writes_quota() -> None:
    """cmd_quota --set calls write_pipeline_quota for a valid period."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pipeline.cli import cmd_quota

    with patch("fieldkit.commands.pipeline.cli.write_pipeline_quota") as mock_write:
        result = CliRunner().invoke(cmd_quota, ["--set", "500000", "--period", "2026-H2"])

    assert result.exit_code == 0
    mock_write.assert_called_once_with(target=500_000, period="2026-H2")


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "period",
    [
        "2026-H3",
        "2026-H4",
        "0000-H1",
        "\uff12\uff10\uff12\uff16-H1",
        "26-H1",
        "10000-Q4",
        "2026-Q0",
        "2026-H2junk",
        " 2026-H1",
        "private-period-sentinel",
    ],
)
def test_quota_set_rejects_invalid_period_before_write(
    period: str, as_json: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main

    writer = MagicMock()
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.write_pipeline_quota", writer)

    argv = ["pipeline", "quota", "--set", "100000", "--period", period]
    if as_json:
        argv.append("--json")

    result = main(argv)

    assert result == 3
    writer.assert_not_called()
    output = capsys.readouterr()
    if as_json:
        assert json.loads(output.out) == {"outcome": "invalid", "error": "invalid_usage", "exit_code": 3}
        assert not output.err
    else:
        assert not output.out
        assert "Invalid quota period" in output.err
    assert period not in output.out
    assert period not in output.err
    assert "Traceback" not in output.err


@pytest.mark.unit
@pytest.mark.parametrize("unit", ["H1", "H2", "Q1", "Q2", "Q3", "Q4"])
def test_quota_set_accepts_each_calendar_unit(unit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.__main__ import main

    writer = MagicMock()
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.write_pipeline_quota", writer)

    result = main(["pipeline", "quota", "--set", "100000", "--period", f"2026-{unit}"])

    assert result == 0
    writer.assert_called_once_with(target=100000, period=f"2026-{unit}")


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("source", ["pursuits", "sf"])
@pytest.mark.parametrize("period", ["2026-H3", "0000-Q1", "\uff12\uff10\uff12\uff16-H1", "private-period-sentinel"])
def test_invalid_stored_quota_period_stops_before_collection(
    tmp_path: Path,
    period: str,
    source: str,
    as_json: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit import config
    from fieldkit.__main__ import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"pipeline:\n  quota:\n    target: 100000\n    period: '{period}'\n", encoding="utf-8")
    monkeypatch.setattr("fieldkit.config._loader.CONFIG_PATH", config_path)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    collector = MagicMock(return_value=[])
    provider = MagicMock(return_value=0.0)
    workspace = MagicMock(return_value=tmp_path)
    writer = MagicMock()
    monkeypatch.setattr("fieldkit.pipeline.quota._collect_pursuits_for_quota", collector)
    monkeypatch.setattr("fieldkit.sf.quota.fetch_sf_closed_won", provider)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.get_fieldkit_home", workspace)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.write_pipeline_quota", writer)
    config.clear_config_caches()
    argv = ["pipeline", "quota", "--source", source]
    if as_json:
        argv.append("--json")

    result = main(argv)

    assert result == 3
    collector.assert_not_called()
    provider.assert_not_called()
    workspace.assert_not_called()
    writer.assert_not_called()
    output = capsys.readouterr()
    assert not output.out
    assert "Invalid quota period" in output.err
    assert period not in output.err
    assert "Traceback" not in output.err


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("period", [None, ""])
def test_quota_report_preserves_optional_period(
    tmp_path: Path, period: str | None, as_json: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.__main__ import main

    quota: dict[str, object] = {"target": 100000}
    if period is not None:
        quota["period"] = period
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.get_pipeline_quota", lambda: quota)
    monkeypatch.setattr("fieldkit.commands.pipeline.cli.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.pipeline.quota._collect_pursuits_for_quota", lambda *args, **kwargs: [])
    argv = ["pipeline", "quota"]
    if as_json:
        argv.append("--json")

    result = main(argv)

    assert result == 0
