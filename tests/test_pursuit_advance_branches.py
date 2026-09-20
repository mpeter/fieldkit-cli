"""Tests for fieldkit.pursuit.advance_cmd — branch coverage for advance_cmd."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.pursuit.advance_cmd import (
    _apply_transition,
    _check_gate,
    _next_stage,
    _read_frontmatter,
    advance_cmd,
)
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit import load_pursuit, write_frontmatter, write_frontmatter_raw

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pursuit(
    tmp_path: Path,
    stage: str = "discover",
    gate_status: str = "pending",
    meddpicc: dict[str, int] | None = None,
    name: str = "deal.md",
) -> Path:
    """Create a minimal pursuit file with YAML frontmatter."""
    fm: dict[str, Any] = {
        "stage": stage,
        "gate-status": gate_status,
        "meddpicc": meddpicc or {},
    }
    content = f"---\n{yaml.dump(fm)}---\n\n# Deal Title\n"
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _next_stage
# ---------------------------------------------------------------------------


# ── TestNextStage (flattened) ───────────────────────────────────────────────


def test_next_stage_pre_pipeline_to_prospect() -> None:
    assert _next_stage("pre-pipeline") == "prospect"


def test_next_stage_negotiate_to_closed_won() -> None:
    assert _next_stage("negotiate") == "closed-won"


def test_next_stage_unknown_returns_none() -> None:
    assert _next_stage("foo-stage") is None


def test_next_stage_discover_to_validate() -> None:
    assert _next_stage("discover") == "validate"


# ---------------------------------------------------------------------------
# _check_gate
# ---------------------------------------------------------------------------


# ── TestCheckGate (flattened) ───────────────────────────────────────────────


def test_check_gate_closed_lost_always_passes() -> None:
    decision = _check_gate("any-stage", "closed-lost")
    assert decision.passed is True
    assert decision.reasons == ()


def test_check_gate_no_gate_defined_passes() -> None:
    # Early stage transition with no gate
    decision = _check_gate("prospect", "qualify")
    assert decision.passed is True


def test_check_gate_gate1_is_pending_despite_legacy_scores() -> None:
    decision = _check_gate("discover", "validate")
    assert decision.passed is False
    assert decision.status == "pending"


def test_check_gate_gate2_is_pending() -> None:
    decision = _check_gate("validate", "propose")
    assert decision.passed is False
    assert decision.status == "pending"


def test_check_gate_gate3_is_pending() -> None:
    decision = _check_gate("propose", "negotiate")
    assert decision.passed is False
    assert decision.status == "pending"


def test_check_gate_negotiate_to_closed_won_no_gate() -> None:
    decision = _check_gate("negotiate", "closed-won")
    assert decision.passed is True
    assert decision.reasons == ()


# ---------------------------------------------------------------------------
# _read_frontmatter / write_frontmatter_raw (historic regression: private _write_frontmatter removed)
# ---------------------------------------------------------------------------


# ── TestReadWriteFrontmatter (flattened) ────────────────────────────────────


def test_read_frontmatter_reads_stage(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="propose")
    fm, _body = _read_frontmatter(path)
    assert fm["stage"] == "propose"


def test_read_frontmatter_raises_on_no_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "nodash.md"
    path.write_text("# Just a heading\nNo frontmatter here.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No YAML frontmatter"):
        _read_frontmatter(path)


def test_read_frontmatter_write_round_trips(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    fm["stage"] = "validate"
    # historic regression: private _write_frontmatter removed; use canonical write_frontmatter_raw.
    write_frontmatter_raw(path, fm, body)
    fm2, _ = _read_frontmatter(path)
    assert fm2["stage"] == "validate"


# ---------------------------------------------------------------------------
# _apply_transition
# ---------------------------------------------------------------------------


# ── TestApplyTransition (flattened) ─────────────────────────────────────────


def test_apply_transition_updates_stage_and_gate_status(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    _apply_transition(path, fm, body, "validate", "pass", "Gate passed")
    fm2, _ = _read_frontmatter(path)
    assert fm2["stage"] == "validate"
    assert fm2["gate-status"] == "pass"


def test_apply_transition_appends_history_entry(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    _apply_transition(path, fm, body, "validate", "pass", "Test note")
    fm2, _ = _read_frontmatter(path)
    history = fm2.get("transition-history", [])
    assert len(history) == 1
    assert history[0]["to"] == "validate"
    assert history[0]["gate-result"] == "pass"
    assert "note" not in history[0]


def test_apply_transition_override_reason_survives_typed_round_trip(tmp_path: Path) -> None:
    """Canonical history fields retain an explicit override through unrelated typed writes."""
    path = _make_pursuit(tmp_path, stage="discover")
    fm, body = _read_frontmatter(path)
    _apply_transition(path, fm, body, "validate", "override", "Override: VP approved verbally")

    model, typed_body, mtime = load_pursuit(path)
    write_frontmatter(path, model, typed_body, expected_mtime=mtime)

    written, _ = _read_frontmatter(path)
    assert written["transition-history"][-1]["gate-result"] == "override"
    assert written["transition-history"][-1]["override-reason"] == "VP approved verbally"


def test_legacy_override_history_migrates_during_typed_round_trip(tmp_path: Path) -> None:
    """Typed round trips canonicalize legacy override provenance."""
    path = _make_pursuit(tmp_path, stage="validate")
    fm, body = _read_frontmatter(path)
    fm["transition-history"] = [
        {
            "date": "2026-09-01",
            "from": "discover",
            "to": "validate",
            "gate-status": "override",
            "note": "Override: VP approved verbally",
        }
    ]
    write_frontmatter_raw(path, fm, body)

    model, typed_body, mtime = load_pursuit(path)
    write_frontmatter(path, model, typed_body, expected_mtime=mtime)

    written, _ = _read_frontmatter(path)
    entry = written["transition-history"][0]
    assert entry["gate-result"] == "override"
    assert entry["override-reason"] == "VP approved verbally"
    assert "gate-status" not in entry
    assert "note" not in entry


def test_legacy_history_preserves_unrecognized_note_during_typed_round_trip(tmp_path: Path) -> None:
    """Typed round trips retain unrelated historical notes."""
    path = _make_pursuit(tmp_path, stage="validate")
    fm, body = _read_frontmatter(path)
    fm["transition-history"] = [{"stage": "discover", "date": "2026-09-01", "note": "Imported manually"}]
    write_frontmatter_raw(path, fm, body)

    model, typed_body, mtime = load_pursuit(path)
    write_frontmatter(path, model, typed_body, expected_mtime=mtime)

    written, _ = _read_frontmatter(path)
    assert written["transition-history"][0]["note"] == "Imported manually"


@pytest.mark.parametrize(
    "entry, message",
    [
        (
            {"stage": "discover", "gate-status": "pass", "gate-result": "override"},
            "conflicting gate-status and gate-result",
        ),
        (
            {
                "stage": "discover",
                "gate-result": "override",
                "note": "Override: Legacy reason",
                "override-reason": "Canonical reason",
            },
            "conflicting note and override-reason",
        ),
    ],
)
def test_typed_load_rejects_conflicting_transition_provenance(
    tmp_path: Path, entry: dict[str, str], message: str
) -> None:
    """Typed loads reject conflicting legacy and canonical provenance."""
    path = _make_pursuit(tmp_path, stage="validate")
    fm, body = _read_frontmatter(path)
    fm["transition-history"] = [entry]
    write_frontmatter_raw(path, fm, body)

    with pytest.raises(ValueError, match=message):
        load_pursuit(path)


def test_apply_transition_refuses_stale_frontmatter(tmp_path: Path) -> None:
    """Transitions refuse to overwrite a pursuit changed after reading."""
    path = _make_pursuit(tmp_path, stage="discover")
    expected_mtime = path.stat().st_mtime
    fm, body = _read_frontmatter(path)
    path.write_text(path.read_text(encoding="utf-8") + "Concurrent note\n", encoding="utf-8")
    concurrent = path.read_text(encoding="utf-8")

    with pytest.raises(FrontmatterStalenessError, match="modified since last read"):
        _apply_transition(
            path,
            fm,
            body,
            "validate",
            "pass",
            "Gate passed",
            expected_mtime=expected_mtime,
        )

    assert path.read_text(encoding="utf-8") == concurrent


def test_apply_transition_invalid_gate_status_raises(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    with pytest.raises(ValueError, match="Invalid gate_status"):
        _apply_transition(path, fm, body, "validate", "bogus-status", "Note")


def test_apply_transition_history_list_init_when_none(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    fm["transition-history"] = None  # force None
    _apply_transition(path, fm, body, "validate", "pass", "Note")
    fm2, _ = _read_frontmatter(path)
    assert isinstance(fm2["transition-history"], list)


def test_apply_transition_history_list_init_when_wrong_type(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="qualify")
    fm, body = _read_frontmatter(path)
    fm["transition-history"] = "not-a-list"
    _apply_transition(path, fm, body, "validate", "pass", "Note")
    fm2, _ = _read_frontmatter(path)
    assert isinstance(fm2["transition-history"], list)


# ---------------------------------------------------------------------------
# advance_cmd CLI
# ---------------------------------------------------------------------------


# ── TestAdvanceCmdCli (flattened) ───────────────────────────────────────────


def test_advance_cmd_legacy_scores_leave_gate_pending(tmp_path: Path) -> None:
    path = _make_pursuit(
        tmp_path,
        stage="discover",
        meddpicc={"identify-pain": 1, "champion": 1, "metrics": 1},
    )
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "validate"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "PENDING" in result.output
    fm, _ = _read_frontmatter(path)
    assert fm["stage"] == "discover"


def test_advance_cmd_gate_fail_exits_1(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="discover", meddpicc={})
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "validate"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "PENDING" in result.output
    fm, _ = _read_frontmatter(path)
    assert fm["stage"] == "discover"  # unchanged


def test_advance_cmd_override_advances_despite_failure(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="discover", meddpicc={})
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        [str(path), "--to", "validate", "--override", "QBR pressure"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    fm, _ = _read_frontmatter(path)
    assert fm["stage"] == "validate"
    assert fm["gate-status"] == "override"


def test_advance_cmd_dry_run_does_not_write(tmp_path: Path) -> None:
    path = _make_pursuit(
        tmp_path,
        stage="discover",
        meddpicc={"identify-pain": 1, "champion": 1, "metrics": 1},
    )
    runner = CliRunner()
    result = runner.invoke(
        advance_cmd,
        [str(path), "--to", "validate", "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "dry-run" in result.output.lower()
    fm, _ = _read_frontmatter(path)
    assert fm["stage"] == "discover"  # unchanged


def test_advance_cmd_invalid_target_stage_exits_3(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="discover")
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "fake-stage"], catch_exceptions=False)
    assert result.exit_code == 3


def test_advance_cmd_same_stage_target_exits_3(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="discover")
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "discover"], catch_exceptions=False)
    assert result.exit_code == 3


def test_advance_cmd_unknown_current_stage_exits_3(tmp_path: Path) -> None:
    """No explicit --to and current stage is unknown → no next stage → exit 3."""
    path = _make_pursuit(tmp_path, stage="nonexistent-stage")
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path)], catch_exceptions=False)
    assert result.exit_code == 3


def test_advance_cmd_unknown_current_stage_with_explicit_target_exits_3(tmp_path: Path) -> None:
    """An explicit target cannot let malformed current stage data bypass validation."""
    path = _make_pursuit(tmp_path, stage="nonexistent-stage")
    original = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(advance_cmd, [str(path), "--to", "validate"], catch_exceptions=False)

    assert result.exit_code == 3
    assert "nonexistent-stage" in result.output
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("stage", ["closed-won", "closed-lost", "won-lost"])
def test_advance_cmd_terminal_current_stage_cannot_reopen(tmp_path: Path, stage: str) -> None:
    """Terminal pursuits remain terminal even when an explicit active target is supplied."""
    path = _make_pursuit(tmp_path, stage=stage)
    original = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(advance_cmd, [str(path), "--to", "validate", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 3
    assert "closed" in result.output.lower()
    assert path.read_text(encoding="utf-8") == original


def test_advance_cmd_backward_transition_requires_override_reason(tmp_path: Path) -> None:
    """Backward transitions require an explicit override reason."""
    path = _make_pursuit(tmp_path, stage="validate")
    original = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(advance_cmd, [str(path), "--to", "discover"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "backward" in result.output.lower()
    assert path.read_text(encoding="utf-8") == original


def test_advance_cmd_backward_transition_records_explicit_override(tmp_path: Path) -> None:
    """Backward transition overrides retain their explicit reason."""
    path = _make_pursuit(tmp_path, stage="validate")

    result = CliRunner().invoke(
        advance_cmd,
        [str(path), "--to", "discover", "--override", "Re-enter discovery"],
        catch_exceptions=False,
    )

    written, _ = _read_frontmatter(path)
    entry = written["transition-history"][-1]
    assert result.exit_code == 0
    assert entry["gate-result"] == "override"
    assert entry["override-reason"] == "Re-enter discovery"


def test_advance_cmd_rejects_whitespace_only_override(tmp_path: Path) -> None:
    """Whitespace-only override reasons are invalid."""
    path = _make_pursuit(tmp_path, stage="discover")
    original = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(
        advance_cmd,
        [str(path), "--to", "validate", "--override", "   "],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert path.read_text(encoding="utf-8") == original


def test_advance_cmd_meddpicc_none_treated_as_empty(tmp_path: Path) -> None:
    """A former null scorecard cannot change the pending policy decision."""
    path = tmp_path / "null-meddpicc.md"
    content = "---\nstage: discover\nmeddpicc: null\n---\n\n# Deal\n"
    path.write_text(content, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "validate"], catch_exceptions=False)
    # Native policy remains pending regardless of historical frontmatter.
    assert result.exit_code == 1


def test_advance_cmd_closed_lost_always_passes(tmp_path: Path) -> None:
    path = _make_pursuit(tmp_path, stage="discover", meddpicc={})
    runner = CliRunner()
    result = runner.invoke(advance_cmd, [str(path), "--to", "closed-lost"], catch_exceptions=False)
    assert result.exit_code == 0
    fm, _ = _read_frontmatter(path)
    assert fm["stage"] == "closed-lost"
