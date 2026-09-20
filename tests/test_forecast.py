"""Tests for fieldkit.pursuit.forecast — weighted pipeline forecast."""

from datetime import date
from pathlib import Path

import pytest

from fieldkit.commands.pursuit.forecast import _parse_acv, compute_forecast

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_FM = """\
---
stage: {stage}
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
sf_consulting_acv: {acv}
sf_arr: ""
sf_owner: Jane Doe
sf_next_steps: next step
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit
"""

TODAY = date(2026, 6, 1)


def _make_account(tmp_path: Path, deals: list[tuple[str, str, str]]) -> Path:
    """Create pursuit files. deals = [(account, name, content)]"""
    for account, name, content in deals:
        p = tmp_path / "accounts" / account / "pursuits"
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{name}.md").write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# _parse_acv
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_acv_dollar_string() -> None:
    assert _parse_acv("$828,495.00") == pytest.approx(828495.0)


@pytest.mark.unit
def test_parse_acv_plain_float() -> None:
    assert _parse_acv("3000000.0") == pytest.approx(3000000.0)


@pytest.mark.unit
def test_parse_acv_numeric() -> None:
    assert _parse_acv(500000) == pytest.approx(500000.0)


@pytest.mark.unit
def test_parse_acv_empty() -> None:
    assert _parse_acv("") == 0.0
    assert _parse_acv(None) == 0.0


@pytest.mark.unit
def test_parse_acv_invalid() -> None:
    assert _parse_acv("N/A") == 0.0


# ---------------------------------------------------------------------------
# compute_forecast — basic scenarios
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_single_discover_deal(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="discover", acv="1000000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.deals) == 1
    assert result.deals[0].weight == pytest.approx(0.10)
    assert result.weighted == pytest.approx(100000.0)
    assert result.commit == pytest.approx(0.0)
    assert result.best_case == pytest.approx(1000000.0)


@pytest.mark.unit
def test_forecast_negotiate_goes_to_commit(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="negotiate", acv="500000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.commit == pytest.approx(500000.0)
    assert result.weighted == pytest.approx(375000.0)  # 0.75 * 500k


@pytest.mark.unit
def test_forecast_closed_won_counted(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="closed-won", acv="200000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.closed_won == pytest.approx(200000.0)
    assert result.commit == pytest.approx(200000.0)
    assert result.weighted == pytest.approx(200000.0)  # weight=1.0


@pytest.mark.unit
def test_forecast_closed_lost_excluded(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="closed-lost", acv="500000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals == []
    assert result.weighted == 0.0


@pytest.mark.unit
def test_forecast_pre_pipeline_excluded(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="pre-pipeline", acv="500000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals == []


@pytest.mark.unit
def test_forecast_multiple_stages(tmp_path: Path) -> None:
    deals = [
        ("acme", "deal1", BASE_FM.format(stage="negotiate", acv="400000")),
        ("acme", "deal2", BASE_FM.format(stage="propose", acv="200000")),
        ("acme", "deal3", BASE_FM.format(stage="discover", acv="1000000")),
    ]
    _make_account(tmp_path, deals)
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.deals) == 3
    assert result.commit == pytest.approx(400000.0)
    assert result.best_case == pytest.approx(1600000.0)
    # weighted: 0.75*400k + 0.5*200k + 0.1*1000k = 300k + 100k + 100k = 500k
    assert result.weighted == pytest.approx(500000.0)


# ---------------------------------------------------------------------------
# compute_forecast — quota
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_quota_gap(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="negotiate", acv="800000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, quota=1000000.0, today=TODAY)
    assert result.quota == 1000000.0
    gap = result.quota - result.commit  # type: ignore[operator]
    assert gap == pytest.approx(200000.0)


@pytest.mark.unit
def test_forecast_quota_over(tmp_path: Path) -> None:
    content = BASE_FM.format(stage="negotiate", acv="2000000")
    _make_account(tmp_path, [("acme", "deal", content)])
    result = compute_forecast(tmp_path, quota=1000000.0, today=TODAY)
    gap = result.quota - result.commit  # type: ignore[operator]
    assert gap < 0  # over quota


# ---------------------------------------------------------------------------
# compute_forecast — account filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_account_filter(tmp_path: Path) -> None:
    deals = [
        ("acme", "deal", BASE_FM.format(stage="negotiate", acv="500000")),
        ("globex", "deal", BASE_FM.format(stage="propose", acv="300000")),
    ]
    _make_account(tmp_path, deals)
    result = compute_forecast(tmp_path, account_filter="acme", today=TODAY)
    assert len(result.deals) == 1
    assert result.commit == pytest.approx(500000.0)


# ---------------------------------------------------------------------------
# compute_forecast — sort order
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_sorted_by_weight_then_acv(tmp_path: Path) -> None:
    deals = [
        ("acme", "low_acv_negotiate", BASE_FM.format(stage="negotiate", acv="100000")),
        ("acme", "high_acv_propose", BASE_FM.format(stage="propose", acv="1000000")),
        ("acme", "highest_acv_negotiate", BASE_FM.format(stage="negotiate", acv="900000")),
    ]
    _make_account(tmp_path, deals)
    result = compute_forecast(tmp_path, today=TODAY)
    # negotiate (0.75) before propose (0.5)
    assert result.deals[0].stage == "negotiate"
    assert result.deals[1].stage == "negotiate"
    # within negotiate: higher ACV first
    assert result.deals[0].acv > result.deals[1].acv


# ---------------------------------------------------------------------------
# compute_forecast — empty / no data
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_empty_accounts(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals == []
    assert result.weighted == 0.0


@pytest.mark.unit
def test_forecast_gmail_intel_excluded(tmp_path: Path) -> None:
    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "gmail-intel.md").write_text(BASE_FM.format(stage="negotiate", acv="999999"), encoding="utf-8")
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals == []


# ---------------------------------------------------------------------------
# historic regression: sf_consulting_acv=0.0 must not fall through to sf_acv
# ---------------------------------------------------------------------------

_FM_WITH_ZERO_CONSULTING = """\
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
sf_consulting_acv: 0.0
sf_acv: 500000
sf_arr: 750000
sf_owner: Jane Doe
sf_next_steps: next step
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit
"""


@pytest.mark.unit
def test_forecast_consulting_acv_zero_not_falsy(tmp_path: Path) -> None:
    """historic regression: sf_consulting_acv=0.0 must be used as-is, not fall through to sf_acv."""
    _make_account(tmp_path, [("acme", "deal-zero-consulting", _FM_WITH_ZERO_CONSULTING)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.deals) == 1
    # sf_consulting_acv=0.0 is the first key and is not None → ACV must be 0.0
    # Before the fix, 0.0 was falsy so sf_acv=500000 would be used instead.
    assert result.deals[0].acv == 0.0, f"Expected ACV=0.0 (sf_consulting_acv honoured), got {result.deals[0].acv}"


@pytest.mark.unit
def test_forecast_falls_through_to_sf_acv_when_consulting_absent(tmp_path: Path) -> None:
    """When sf_consulting_acv is absent (None), fall through to sf_acv."""
    fm = """\
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
sf_acv: 500000
sf_owner: Jane Doe
sf_next_steps: next step
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit
"""
    _make_account(tmp_path, [("acme", "deal-no-consulting", fm)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.deals) == 1
    assert result.deals[0].acv == 500000.0


# ---------------------------------------------------------------------------
# historic regression: skipped deals with unrecognized stages surfaced in ForecastResult
# ---------------------------------------------------------------------------

_FM_UNKNOWN_STAGE = """\
---
stage: {stage}
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
sf_stage: Unknown
sf_close_date: 12/31/2027
sf_consulting_acv: 100000
sf_owner: Jane Doe
sf_next_steps: next step
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit
"""


@pytest.mark.unit
def test_forecast_skipped_unknown_stage_captured(tmp_path: Path) -> None:
    """historic regression: deals with unrecognized stages are captured in result.skipped."""
    _make_account(tmp_path, [("acme", "deal-weird", _FM_UNKNOWN_STAGE.format(stage="pipeline"))])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals == [], "Unrecognized stage must not appear in deals"
    assert result.skipped == ["pipeline"], f"Expected ['pipeline'], got {result.skipped}"


@pytest.mark.unit
def test_forecast_skipped_multiple_unknown_stages(tmp_path: Path) -> None:
    """historic regression: multiple unrecognized stages are all captured."""
    _make_account(
        tmp_path,
        [
            ("acme", "deal-a", _FM_UNKNOWN_STAGE.format(stage="pipeline")),
            ("acme", "deal-b", _FM_UNKNOWN_STAGE.format(stage="pipeline")),
            ("acme", "deal-c", _FM_UNKNOWN_STAGE.format(stage="evaluation")),
        ],
    )
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.skipped) == 3
    assert sorted(set(result.skipped)) == ["evaluation", "pipeline"]


@pytest.mark.unit
def test_forecast_skipped_empty_for_known_stages(tmp_path: Path) -> None:
    """historic regression: result.skipped is empty when all stages are recognized."""
    _make_account(tmp_path, [("acme", "deal", BASE_FM.format(stage="discover", acv="100000"))])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.skipped == []


@pytest.mark.unit
def test_forecast_cli_warns_on_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """historic regression: CLI emits WARNING to stderr when deals are skipped."""
    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal-weird.md").write_text(_FM_UNKNOWN_STAGE.format(stage="pipeline"), encoding="utf-8")
    (p / "deal-known.md").write_text(BASE_FM.format(stage="discover", acv="50000"), encoding="utf-8")

    runner = CliRunner()
    import unittest.mock as mock

    with mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    # WARNING is emitted to stderr; CliRunner captures both by default
    assert "WARNING" in result.output
    assert "pipeline" in result.output
    assert "STAGE_WEIGHT" in result.output


@pytest.mark.unit
def test_forecast_cli_marks_closed_won_deal(tmp_path: Path) -> None:
    """implementation change: closed-won deals get a '(won)' marker; other deals do not."""
    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal-won.md").write_text(BASE_FM.format(stage="closed-won", acv="75000"), encoding="utf-8")
    (p / "deal-active.md").write_text(BASE_FM.format(stage="negotiate", acv="50000"), encoding="utf-8")

    runner = CliRunner()
    import unittest.mock as mock

    with mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output.count("(won)") == 1

    lines = result.output.splitlines()
    won_line = next(line for line in lines if "deal-won" in line)
    active_line = next(line for line in lines if "deal-active" in line)
    assert "(won)" in won_line
    assert "(won)" not in active_line


# ---------------------------------------------------------------------------
# implementation change: qualify stage included in STAGE_WEIGHT at 5%
# ---------------------------------------------------------------------------


# ── TestForecastQualifyStage (flattened) ────────────────────────────────────


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_deal_in_result_deals(tmp_path: Path) -> None:
    """qualify-stage pursuit appears in result.deals (not silently dropped)."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert len(result.deals) == 1
    assert result.deals[0].stage == "qualify"


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_deal_weight_is_5_percent(tmp_path: Path) -> None:
    """qualify-stage deal carries weight=0.05."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.deals[0].weight == pytest.approx(0.05)


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_deal_contributes_to_weighted(tmp_path: Path) -> None:
    """qualify-stage deal contributes 5% of ACV to weighted total."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    # 100000 * 0.05 = 5000
    assert result.weighted == pytest.approx(5000.0)


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_deal_contributes_to_best_case(tmp_path: Path) -> None:
    """qualify-stage deal contributes full ACV to best_case total."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.best_case == pytest.approx(100000.0)


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_deal_excluded_from_commit(tmp_path: Path) -> None:
    """qualify-stage deal must NOT appear in commit total (not in COMMIT_STAGES)."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert result.commit == pytest.approx(0.0)


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_qualify_stage_qualify_not_in_skipped(tmp_path: Path) -> None:
    """qualify-stage deal must NOT appear in result.skipped."""
    content = BASE_FM.format(stage="qualify", acv="100000")
    _make_account(tmp_path, [("acme", "deal-qualify", content)])
    result = compute_forecast(tmp_path, today=TODAY)
    assert "qualify" not in result.skipped


# ---------------------------------------------------------------------------
# implementation change: auto-read quota from config
# ---------------------------------------------------------------------------


# ── TestForecastAutoQuota (flattened) ───────────────────────────────────────


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_auto_quota_auto_quota_from_config(tmp_path: Path) -> None:
    """When -q not given, quota is read from get_pipeline_quota()."""
    import unittest.mock as mock

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal.md").write_text(BASE_FM.format(stage="negotiate", acv="500000"), encoding="utf-8")

    runner = CliRunner()
    with (
        mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path),
        mock.patch(
            "fieldkit.commands.pursuit.forecast.get_pipeline_quota",
            return_value={"target": 1000000, "period": "2026-H2"},
        ),
    ):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Quota" in result.output
    assert "$1,000,000" in result.output


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_auto_quota_auto_quota_none_when_not_configured(tmp_path: Path) -> None:
    """When get_pipeline_quota() returns None, no quota section is shown."""
    import unittest.mock as mock

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal.md").write_text(BASE_FM.format(stage="negotiate", acv="500000"), encoding="utf-8")

    runner = CliRunner()
    with (
        mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path),
        mock.patch("fieldkit.commands.pursuit.forecast.get_pipeline_quota", return_value=None),
    ):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Quota" not in result.output


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_auto_quota_explicit_quota_overrides_config(tmp_path: Path) -> None:
    """-q flag overrides config quota."""
    import unittest.mock as mock

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal.md").write_text(BASE_FM.format(stage="negotiate", acv="500000"), encoding="utf-8")

    runner = CliRunner()
    with (
        mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path),
        mock.patch(
            "fieldkit.commands.pursuit.forecast.get_pipeline_quota",
            return_value={"target": 999999},
        ),
    ):
        result = runner.invoke(forecast_cli, ["-q", "2000000"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "$2,000,000" in result.output


# ---------------------------------------------------------------------------
# implementation change: $0 ACV warning
# ---------------------------------------------------------------------------


# ── TestForecastZeroAcvWarning (flattened) ──────────────────────────────────


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_zero_acv_warning_zero_acv_warning_emitted(tmp_path: Path) -> None:
    """Deals with ACV=0.0 trigger a WARNING block."""
    import unittest.mock as mock

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal-zero.md").write_text(BASE_FM.format(stage="discover", acv="0"), encoding="utf-8")
    (p / "deal-ok.md").write_text(BASE_FM.format(stage="negotiate", acv="500000"), encoding="utf-8")

    runner = CliRunner()
    with (
        mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path),
        mock.patch("fieldkit.commands.pursuit.forecast.get_pipeline_quota", return_value=None),
    ):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "WARNING" in result.output
    assert "$0 ACV" in result.output or "0 ACV" in result.output


@pytest.mark.unit
@pytest.mark.unit
def test_forecast_zero_acv_warning_no_warning_when_all_acv_nonzero(tmp_path: Path) -> None:
    """No warning when all deals have non-zero ACV."""
    import unittest.mock as mock

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.forecast import cli as forecast_cli

    p = tmp_path / "accounts" / "acme" / "pursuits"
    p.mkdir(parents=True)
    (p / "deal.md").write_text(BASE_FM.format(stage="negotiate", acv="500000"), encoding="utf-8")

    runner = CliRunner()
    with (
        mock.patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path),
        mock.patch("fieldkit.commands.pursuit.forecast.get_pipeline_quota", return_value=None),
    ):
        result = runner.invoke(forecast_cli, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "$0 ACV" not in result.output
