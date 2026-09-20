"""Contract tests for gate-status validation in the advance_cmd module.

These tests verify two invariants:
1. _apply_transition only ever writes gate-status values from ALLOWED_GATE_STATUSES.
2. The guard in _apply_transition raises ValueError for any invalid gate-status.

Also covers historic regression (invalid stage name rejected) and historic regression (same-stage target rejected).
"""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.pursuit.advance_cmd import _apply_transition, advance_cmd
from fieldkit.pursuit.gate_criteria import ALLOWED_GATE_STATUSES

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_FM = """\
---
stage: discover
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
  metrics: 0
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  identify-pain: 0
  champion: 0
  competition: 0
  paper-process: 0
sf_opportunity_id: OPP123
sf_account_name: ACME Corp
sf_close_date: 2025-12-31
sf_amount: 50000
---
# Test Pursuit
"""


def _make_pursuit(tmp_path: Path, name: str = "pursuit.md") -> Path:
    p = tmp_path / name
    p.write_text(_MINIMAL_FM, encoding="utf-8")
    return p


def _read_gate_status(p: Path) -> str:
    text = p.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1]) or {}
    return str(fm.get("gate-status", ""))


# ---------------------------------------------------------------------------
# Guard: invalid gate_status raises ValueError before writing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", ["approved", "yes", "ok", "", "PASS"])
def test_apply_transition_rejects_invalid_gate_status(tmp_path: Path, bad_status: str) -> None:
    """_apply_transition must raise ValueError for any value not in ALLOWED_GATE_STATUSES."""
    p = _make_pursuit(tmp_path)
    parts = _MINIMAL_FM.split("---", 2)
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2]

    with pytest.raises(ValueError, match="gate_status"):
        _apply_transition(p, fm, body, new_stage="validate", gate_status=bad_status, note="test")

    # File must be unchanged after the guard fires
    assert _read_gate_status(p) == "pending"


# ---------------------------------------------------------------------------
# Contract: only ALLOWED_GATE_STATUSES values are ever written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valid_status", sorted(ALLOWED_GATE_STATUSES))
def test_apply_transition_writes_valid_gate_status(tmp_path: Path, valid_status: str) -> None:
    """_apply_transition must write each allowed gate_status without error."""
    p = _make_pursuit(tmp_path, f"pursuit-{valid_status}.md")
    parts = _MINIMAL_FM.split("---", 2)
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2]

    _apply_transition(p, fm, body, new_stage="validate", gate_status=valid_status, note="test note")

    written = _read_gate_status(p)
    assert written == valid_status, f"Expected gate-status '{valid_status}' to be written, got '{written}'"


# ---------------------------------------------------------------------------
# Advance flow values: only 'pass', 'fail', 'override' are produced by advance_cmd
# ---------------------------------------------------------------------------


def test_advance_gate_statuses_are_subset_of_allowed() -> None:
    """The values emitted by the advance flow must be within ALLOWED_GATE_STATUSES."""
    advance_produced = {"pass", "fail", "override"}
    assert advance_produced <= ALLOWED_GATE_STATUSES, (
        f"advance_cmd produces {advance_produced - ALLOWED_GATE_STATUSES} which are not in ALLOWED_GATE_STATUSES"
    )


# ---------------------------------------------------------------------------
# historic regression: advance rejects unknown target stage
# ---------------------------------------------------------------------------

_DISCOVER_FM = """\
---
stage: discover
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
  metrics: 0
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  identify-pain: 0
  champion: 0
  competition: 0
  paper-process: 0
sf_opportunity_id: OPP123
sf_close_date: 2025-12-31
---
# Test Pursuit
"""


def _make_discover_pursuit(tmp_path: Path, name: str = "pursuit.md") -> Path:
    p = tmp_path / name
    p.write_text(_DISCOVER_FM, encoding="utf-8")
    return p


# ── TestAdvanceInvalidStage (flattened) ─────────────────────────────────────


@pytest.mark.unit
def test_next_stage_invalid_stage_exits_3(tmp_path: Path) -> None:
    """Completely unknown stage name → exit 3."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "bogus-stage"], catch_exceptions=False)
    assert result.exit_code == 3


@pytest.mark.unit
def test_next_stage_invalid_stage_names_invalid_value_in_stderr(tmp_path: Path) -> None:
    """Error output must name the invalid stage value."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "not-a-stage"], catch_exceptions=False)
    assert "not-a-stage" in result.output


@pytest.mark.unit
def test_next_stage_invalid_stage_lists_valid_stages_in_stderr(tmp_path: Path) -> None:
    """Error output must list the valid stages so the AE knows what to use."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "bogus-stage"], catch_exceptions=False)
    # At least one known stage name must appear in the error output
    assert "discover" in result.output or "validate" in result.output


@pytest.mark.unit
def test_next_stage_invalid_stage_file_unmodified(tmp_path: Path) -> None:
    """Pursuit file must not be touched when stage name is invalid."""
    p = _make_discover_pursuit(tmp_path)
    original = p.read_text(encoding="utf-8")
    runner = CliRunner()
    runner.invoke(advance_cmd, [str(p), "--to", "bogus-stage"], catch_exceptions=False)
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_next_stage_valid_stage_proceeds_to_gate_check(tmp_path: Path) -> None:
    """A valid stage name must not be rejected by the invalid-stage guard."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    # validate is a valid stage; native qualification policy is pending → exit 1, not 3
    result = runner.invoke(advance_cmd, [str(p), "--to", "validate"], catch_exceptions=False)
    # exit 1 = gate pending (not the invalid-stage guard)
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# historic regression: advance rejects same-stage target
# ---------------------------------------------------------------------------


# ── TestAdvanceSameStage (flattened) ────────────────────────────────────────


@pytest.mark.unit
def test_next_stage_same_stage_exits_3(tmp_path: Path) -> None:
    """Target == current stage → exit 3."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "discover"], catch_exceptions=False)
    assert result.exit_code == 3


@pytest.mark.unit
def test_next_stage_same_stage_error_mentions_stage(tmp_path: Path) -> None:
    """Error output must name the duplicate stage."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "discover"], catch_exceptions=False)
    assert "discover" in result.output


@pytest.mark.unit
def test_next_stage_same_stage_file_unmodified(tmp_path: Path) -> None:
    """Pursuit file must not be touched when target equals current stage."""
    p = _make_discover_pursuit(tmp_path)
    original = p.read_text(encoding="utf-8")
    runner = CliRunner()
    runner.invoke(advance_cmd, [str(p), "--to", "discover"], catch_exceptions=False)
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_next_stage_different_stage_does_not_trigger_same_stage_guard(tmp_path: Path) -> None:
    """A different valid stage must not be rejected by the same-stage guard."""
    p = _make_discover_pursuit(tmp_path)
    runner = CliRunner()
    # validate is different from discover; native qualification policy is pending → exit 1
    result = runner.invoke(advance_cmd, [str(p), "--to", "validate"], catch_exceptions=False)
    assert result.exit_code != 3 or "Already at stage" not in result.output


# ---------------------------------------------------------------------------
# Task 10.3 — gate pass / pending exit codes
# ---------------------------------------------------------------------------

_PASSING_FM = """\
---
stage: discover
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
  metrics: 1
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  identify-pain: 1
  champion: 1
  competition: 0
  paper-process: 0
sf_opportunity_id: OPP-PASS
sf_close_date: 2027-12-31
---
# Gate Pass Pursuit
"""

_FAILING_FM = """\
---
stage: discover
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
  metrics: 0
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  identify-pain: 0
  champion: 0
  competition: 0
  paper-process: 0
sf_opportunity_id: OPP-FAIL
sf_close_date: 2027-12-31
---
# Gate Fail Pursuit
"""


@pytest.mark.unit
def test_advance_cmd_exits_nonzero_gate_pending(tmp_path: Path) -> None:
    """advance_cmd exits 1 while native policy is unratified."""
    p = tmp_path / "pursuit.md"
    p.write_text(_FAILING_FM, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "validate"], catch_exceptions=False)

    assert result.exit_code == 1, f"Expected exit 1 (gate pending), got {result.exit_code}:\n{result.output}"
    assert "PENDING" in result.output, "Output must indicate pending native policy"


@pytest.mark.unit
def test_advance_cmd_legacy_passing_scores_do_not_advance(tmp_path: Path) -> None:
    """Historical scores cannot pass an unratified native gate."""
    p = tmp_path / "pursuit.md"
    p.write_text(_PASSING_FM, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(p), "--to", "validate"], catch_exceptions=False)

    assert result.exit_code == 1, f"Expected exit 1 (gate pending), got {result.exit_code}:\n{result.output}"
    assert "PENDING" in result.output

    # Verify the pending decision left the file unchanged.
    updated = p.read_text(encoding="utf-8")
    assert "stage: discover" in updated, "Pursuit file must remain at 'discover'"


# ---------------------------------------------------------------------------
# implementation change: --account / --name flags as alternative to positional PURSUIT_SPEC
# ---------------------------------------------------------------------------


# ── TestAccountNameFlags (flattened) ────────────────────────────────────────


def _account_name_flags_make_pursuit(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal pursuit file at accounts/acme/pursuits/deal.md."""
    pursuits_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    p = pursuits_dir / "deal.md"
    p.write_text(_MINIMAL_FM + "\n---\nbody\n", encoding="utf-8")
    return tmp_path, p


def test_account_name_flags_account_and_name_resolve_to_same_result_as_positional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--account acme --name deal should resolve the same pursuit as 'acme/deal'."""
    accounts_root, _p = _account_name_flags_make_pursuit(tmp_path)
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root / "accounts",
    )
    runner = CliRunner()

    result_positional = runner.invoke(advance_cmd, ["acme/deal", "--dry-run"], catch_exceptions=False)
    result_flags = runner.invoke(
        advance_cmd, ["--account", "acme", "--name", "deal", "--dry-run"], catch_exceptions=False
    )

    assert result_positional.exit_code == result_flags.exit_code
    assert "Pursuit:" in result_flags.output
    assert "deal" in result_flags.output


def test_account_name_flags_account_without_name_raises_usage_error(tmp_path: Path) -> None:
    """--account without --name must raise UsageError."""
    runner = CliRunner()
    result = runner.invoke(advance_cmd, ["--account", "acme", "--dry-run"])
    assert result.exit_code != 0
    assert "--account and --name must be used together" in result.output


def test_account_name_flags_name_without_account_raises_usage_error(tmp_path: Path) -> None:
    """--name without --account must raise UsageError."""
    runner = CliRunner()
    result = runner.invoke(advance_cmd, ["--name", "deal", "--dry-run"])
    assert result.exit_code != 0
    assert "--account and --name must be used together" in result.output


def test_account_name_flags_both_positional_and_flags_raises_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Providing both positional PURSUIT_SPEC and --account/--name must raise UsageError."""
    accounts_root, _ = _account_name_flags_make_pursuit(tmp_path)
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root / "accounts",
    )
    runner = CliRunner()
    result = runner.invoke(advance_cmd, ["acme/deal", "--account", "acme", "--name", "deal", "--dry-run"])
    assert result.exit_code != 0
    assert "not both" in result.output


def test_account_name_flags_no_args_raises_usage_error(tmp_path: Path) -> None:
    """No PURSUIT_SPEC and no --account/--name must raise UsageError."""
    runner = CliRunner()
    result = runner.invoke(advance_cmd, ["--dry-run"])
    assert result.exit_code != 0
    assert "Provide PURSUIT_SPEC" in result.output


# ---------------------------------------------------------------------------
# Helpers for accounts_root-based tests (implementation change)
# ---------------------------------------------------------------------------


def _make_pursuit_in(
    accounts_root: Path,
    account: str = "acme",
    slug: str = "deal",
    stage: str = "discover",
    meddpicc_scores: dict[str, int] | None = None,
) -> Path:
    """Create a pursuit file under accounts_root/<account>/pursuits/<slug>.md."""
    scores = meddpicc_scores or {
        "metrics": 2,
        "economic-buyer": 2,
        "decision-criteria": 2,
        "decision-process": 2,
        "identify-pain": 2,
        "champion": 2,
        "competition": 2,
        "paper-process": 2,
    }
    meddpicc_yaml = "\n".join(f"  {k}: {v}" for k, v in scores.items())
    pursuits_dir = accounts_root / account / "pursuits"
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    p = pursuits_dir / f"{slug}.md"
    p.write_text(
        f"""---
stage: {stage}
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
{meddpicc_yaml}
---
body
""",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# implementation change: --account/--name full flow (pass, pending, override, dry-run)
# ---------------------------------------------------------------------------


# ── TestAccountNameFlow (flattened) ─────────────────────────────────────────


@pytest.mark.unit
def test_account_name_flow_account_name_gate_pending_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--account/--name preserves a pending native-policy decision."""
    accounts_root = tmp_path / "accounts"
    _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="discover")
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal", "--to", "validate"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\n{result.output}"
    assert "PENDING" in result.output
    updated = (accounts_root / "acme" / "pursuits" / "deal.md").read_text(encoding="utf-8")
    assert "stage: discover" in updated


@pytest.mark.unit
def test_account_name_flow_legacy_low_scores_also_report_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy values do not change the pending decision."""
    accounts_root = tmp_path / "accounts"
    low_scores = dict.fromkeys(
        [
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "identify-pain",
            "champion",
            "competition",
            "paper-process",
        ],
        0,
    )
    p = _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="discover", meddpicc_scores=low_scores)
    original = p.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal", "--to", "validate"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}:\n{result.output}"
    assert "PENDING" in result.output
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_account_name_flow_account_name_override_advances_on_gate_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--override REASON advances a pending gate; gate-status becomes 'override'."""
    accounts_root = tmp_path / "accounts"
    low_scores = dict.fromkeys(
        [
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "identify-pain",
            "champion",
            "competition",
            "paper-process",
        ],
        0,
    )
    p = _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="discover", meddpicc_scores=low_scores)
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal", "--to", "validate", "--override", "exec decision"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"Expected exit 0 with override, got {result.exit_code}:\n{result.output}"
    assert "Override" in result.output
    updated = p.read_text(encoding="utf-8")
    assert "stage: validate" in updated
    assert "gate-status: override" in updated


@pytest.mark.unit
def test_account_name_flow_account_name_dry_run_gate_pending_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run reports pending and does not write."""
    accounts_root = tmp_path / "accounts"
    p = _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="discover")
    original = p.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal", "--to", "validate", "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, f"Expected exit 1 (dry-run pending), got {result.exit_code}:\n{result.output}"
    assert "dry-run" in result.output
    # File must be untouched
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_account_name_flow_account_name_dry_run_gate_fail_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run with failing gate → exit 1, file NOT written."""
    accounts_root = tmp_path / "accounts"
    low_scores = dict.fromkeys(
        [
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "identify-pain",
            "champion",
            "competition",
            "paper-process",
        ],
        0,
    )
    p = _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="discover", meddpicc_scores=low_scores)
    original = p.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal", "--to", "validate", "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, f"Expected exit 1 (dry-run fail), got {result.exit_code}:\n{result.output}"
    assert "dry-run" in result.output
    assert p.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# _resolve_target_stage returning None → exit 3
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_advance_cmd_no_next_stage_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pursuit at a stage with no defined next stage → exit 3.

    'closed-won' is not in PIPELINE_STAGES so _next_stage raises ValueError
    and returns None, causing _resolve_target_stage to return None.
    """
    accounts_root = tmp_path / "accounts"
    _ = _make_pursuit_in(accounts_root, account="acme", slug="deal", stage="closed-won")
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: accounts_root,
    )
    runner = CliRunner()
    # No --to: relies on auto-resolution which will find no next stage
    result = runner.invoke(
        advance_cmd,
        ["--account", "acme", "--name", "deal"],
        catch_exceptions=False,
    )
    assert result.exit_code == 3, f"Expected exit 3 (no next stage), got {result.exit_code}:\n{result.output}"
    assert "no defined next stage" in result.output


# ---------------------------------------------------------------------------
# Override path via positional spec (gate pending, override advances)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_override_path_positional_spec(tmp_path: Path) -> None:
    """--override with a positional full-path spec and failing gate → exit 0, file updated."""
    low_scores = dict.fromkeys(
        [
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "identify-pain",
            "champion",
            "competition",
            "paper-process",
        ],
        0,
    )
    # Historical values do not affect the pending policy decision.
    meddpicc_yaml = "\n".join(f"  {k}: {v}" for k, v in low_scores.items())
    p = tmp_path / "pursuit.md"
    p.write_text(
        f"""---
stage: discover
gate-status: pending
last-transition: 2024-01-01
transition-history: []
meddpicc:
{meddpicc_yaml}
---
body
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        [str(p), "--to", "validate", "--override", "VP approved verbally"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"Expected exit 0 with override, got {result.exit_code}:\n{result.output}"
    assert "Override" in result.output
    updated = p.read_text(encoding="utf-8")
    assert "stage: validate" in updated
    assert "gate-status: override" in updated
