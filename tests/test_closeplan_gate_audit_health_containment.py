"""Phase B regression coverage for legacy qualification consumers."""

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.advance_cmd import advance_cmd
from fieldkit.commands.pursuit.audit import audit_file
from fieldkit.commands.pursuit.pipeline_health import classify_pursuit

pytestmark = pytest.mark.unit


def _write_legacy_pursuit(tmp_path: Path, *, stage: str = "discover", close_date: str = "2027-12-31") -> Path:
    path = tmp_path / "deal.md"
    path.write_text(
        f"""---
stage: {stage}
gate-status: pending
last-transition: 2026-01-01
transition-history: []
meddpicc:
  metrics: 3
  economic-buyer: 3
  decision-criteria: 3
  decision-process: 3
  identify-pain: 3
  champion: 3
  competition: 3
  paper-process: 3
  composite: 24/24
sf_opportunity_id: OPP-EXAMPLE
sf_stage: Discover
sf_close_date: "{close_date}"
sf_next_steps: Review next step
---

# Example pursuit
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("stage", "target"),
    [("discover", "validate"), ("validate", "propose"), ("propose", "negotiate")],
)
def test_legacy_scores_never_pass_unratified_native_gate(tmp_path: Path, stage: str, target: str) -> None:
    pursuit = _write_legacy_pursuit(tmp_path, stage=stage)

    result = CliRunner().invoke(advance_cmd, [str(pursuit), "--to", target, "--dry-run", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["gate_status"] == "pending"
    assert payload["gate_passed"] is False
    assert payload["reasons"] == ["Salesforce-native qualification policy is not yet ratified for this transition"]


@pytest.mark.parametrize(
    ("stage", "target"),
    [("qualify", "discover"), ("negotiate", "closed-won"), ("discover", "closed-lost")],
)
def test_qualification_independent_transitions_remain_available(tmp_path: Path, stage: str, target: str) -> None:
    pursuit = _write_legacy_pursuit(tmp_path, stage=stage)

    result = CliRunner().invoke(advance_cmd, [str(pursuit), "--to", target, "--dry-run", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["gate_status"] == "pass"
    assert payload["gate_passed"] is True
    assert payload["reasons"] == []


def test_explicit_override_advances_pending_gate_and_preserves_reason(tmp_path: Path) -> None:
    pursuit = _write_legacy_pursuit(tmp_path)

    result = CliRunner().invoke(
        advance_cmd,
        [str(pursuit), "--to", "validate", "--override", "Reviewed by sales leadership", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["gate_status"] == "override"
    assert payload["override"] == "Reviewed by sales leadership"
    assert payload["advanced"] is True
    assert "Reviewed by sales leadership" in pursuit.read_text(encoding="utf-8")


def test_audit_reports_current_qualification_unavailable_without_legacy_conclusions(tmp_path: Path) -> None:
    pursuit = _write_legacy_pursuit(tmp_path)

    result = audit_file(pursuit, today=date(2026, 9, 12))

    assert result.qualification_status == "unavailable"
    messages = [finding.message for finding in result.findings]
    assert not any("/24" in message or "champion" in message.lower() for message in messages)


def test_health_ignores_legacy_values_but_preserves_date_risk(tmp_path: Path) -> None:
    pursuit = _write_legacy_pursuit(tmp_path, close_date="2026-09-01")
    audited = audit_file(pursuit, today=date(2026, 9, 12))

    item = classify_pursuit(audited, date(2026, 9, 12))

    assert item is not None
    assert item.qualification_status == "unavailable"
    assert item.risk_tier == "HIGH"
    assert item.risk_reasons == ["Overdue close date (2026-09-01)"]
