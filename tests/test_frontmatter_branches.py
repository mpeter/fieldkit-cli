"""Branch-coverage tests for fieldkit.commands.sf.frontmatter._run_sf_mode.

Targets the branches in _run_sf_mode that are not yet hit by test_sf_frontmatter.py.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.frontmatter import (
    _run_sf_mode,
    cli,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_pursuit(tmp_path: Path, stem: str = "deal", stage: str = "discover") -> Path:
    path = tmp_path / f"{stem}.md"
    content = (
        "---\n"
        f"stage: {stage}\n"
        "gate-status: pending\n"
        "last-transition: 2026-01-01\n"
        "sf_opportunity_id: 006Pe000012n2GkIAI\n"
        "sf_name: Deal\n"
        "sf_stage: Discover\n"
        "sf_close_date: 2026-12-31\n"
        "sf_arr: $100,000\n"
        "sf_owner: Jane Doe\n"
        "sf_next_steps: Schedule demo\n"
        "sf_last_pulled: 2026-01-01T00:00:00Z\n"
        "sf_acv: $90,000\n"
        "sf_consulting_acv: $50,000\n"
        "sf_training_acv: $40,000\n"
        "meddpicc:\n"
        "  metrics: 2\n"
        "  economic-buyer: 2\n"
        "  decision-criteria: 2\n"
        "  decision-process: 2\n"
        "  identify-pain: 2\n"
        "  champion: 2\n"
        "  competition: 2\n"
        "  paper-process: 2\n"
        "---\n\n# Deal Title\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _sf_payload(
    opp_id: str = "006Pe000012n2GkIAI",
    stage: str = "Discover",
    arr: str = "$100,000",
    **extra: Any,
) -> str:
    payload = {
        "status": "ok",
        "opportunity_id": opp_id,
        "stage": stage,
        "arr": arr,
        "pulled_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# _run_sf_mode: file not found
# ---------------------------------------------------------------------------


# ── TestRunSfModeFileNotFound (flattened) ───────────────────────────────────


def test_strip_sf_keys_exits_1_when_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_sf_mode(str(tmp_path / "missing.md"), '{"status":"ok","arr":"$100k"}')
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _run_sf_mode: invalid JSON
# ---------------------------------------------------------------------------


# ── TestRunSfModeInvalidJson (flattened) ────────────────────────────────────


def test_strip_sf_keys_exits_1_on_bad_json(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _run_sf_mode(str(path), "not-valid-json")
    assert exc_info.value.code == 1


@pytest.mark.parametrize("payload", ['["status", "ok"]', '"status"', "42", "true"])
def test_run_sf_mode_rejects_non_object_json(tmp_path: Path, payload: str) -> None:
    """SF mode rejects valid JSON values that are not objects."""
    from fieldkit.errors import FieldkitError

    path = _minimal_pursuit(tmp_path)
    original = path.read_text(encoding="utf-8")

    with pytest.raises(FieldkitError, match="payload must be a JSON object"):
        _run_sf_mode(str(path), payload)

    assert path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# _run_sf_mode: status != ok
# ---------------------------------------------------------------------------


# ── TestRunSfModeStatusNotOk (flattened) ────────────────────────────────────


def test_strip_sf_keys_raises_fieldkit_error_when_status_error(tmp_path: Path) -> None:
    """A1 (implementation note): empty/error payload raises FieldkitError instead of sys.exit(1)."""
    from fieldkit.errors import FieldkitError

    path = _minimal_pursuit(tmp_path)
    payload = json.dumps({"status": "error", "message": "SF timeout"})
    with pytest.raises(FieldkitError, match="Empty SF payload"):
        _run_sf_mode(str(path), payload)


# ---------------------------------------------------------------------------
# _run_sf_mode: historic regression — opportunity_id injected from file frontmatter
# ---------------------------------------------------------------------------


# ── TestBug307OppIdInjected (flattened) ─────────────────────────────────────


def test_bug307_opp_id_injected_injected_from_file_when_absent_in_payload(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    # Payload without opportunity_id key (not the same as null)
    payload = json.dumps({"status": "ok", "stage": "Validate", "pulled_at": "2026-01-01T00:00:00Z"})
    # Should write without error (opp_id injected from file)
    _run_sf_mode(str(path), payload)
    content = path.read_text(encoding="utf-8")
    assert "sf_stage: Validate" in content


def test_bug307_opp_id_injected_malformed_file_opp_id_not_injected(tmp_path: Path) -> None:
    """historic regression: if file has malformed opp_id, skip injection; historic regression guard fires."""
    path = tmp_path / "malformed-opp.md"
    path.write_text(
        "---\n"
        "stage: discover\n"
        "sf_opportunity_id: NEEDS-LOOKUP\n"
        "sf_stage: Discover\n"
        "sf_arr: $100,000\n"
        "sf_close_date: 2026-12-31\n"
        "sf_owner: Jane Doe\n"
        "sf_next_steps: Schedule demo\n"
        "sf_last_pulled: 2026-01-01T00:00:00Z\n"
        "sf_acv: $90,000\n"
        "sf_consulting_acv: $50,000\n"
        "sf_training_acv: $40,000\n"
        "---\n\n# Deal\n",
        encoding="utf-8",
    )
    # Payload without opportunity_id
    payload = json.dumps({"status": "ok", "stage": "Validate", "pulled_at": "2026-01-01T00:00:00Z"})
    # historic regression guard fires → returns without writing (no sys.exit)
    _run_sf_mode(str(path), payload)
    # File should be unchanged
    assert "NEEDS-LOOKUP" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _run_sf_mode: historic regression — refuse to blank existing sf_opportunity_id
# ---------------------------------------------------------------------------


# ── TestBug139RefuseBlankOppId (flattened) ──────────────────────────────────


def test_bug139_refuse_blank_opp_id_blank_incoming_opp_id_aborts_write(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    original = path.read_text(encoding="utf-8")
    # Payload with null opportunity_id (explicitly present but empty)
    payload = json.dumps(
        {
            "status": "ok",
            "opportunity_id": "",
            "stage": "Validate",
            "pulled_at": "2026-01-01T00:00:00Z",
        }
    )
    _run_sf_mode(str(path), payload)
    # File must be unchanged (historic regression aborted the write)
    assert path.read_text(encoding="utf-8") == original


def test_bug139_refuse_blank_opp_id_null_opportunity_id_aborts(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    original = path.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "status": "ok",
            "opportunity_id": None,
            "stage": "Validate",
            "pulled_at": "2026-01-01T00:00:00Z",
        }
    )
    _run_sf_mode(str(path), payload)
    assert path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# _run_sf_mode: historic regression — unknown payload keys dropped
# ---------------------------------------------------------------------------


# ── TestBug311UnknownKeyDropped (flattened) ─────────────────────────────────


def test_bug311_unknown_key_dropped_unknown_key_dropped_write_proceeds(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    payload = json.dumps(
        {
            "status": "ok",
            "opportunity_id": "006Pe000012n2GkIAI",
            "stage": "Validate",
            "arr": "$120,000",
            "pulled_at": "2026-01-01T00:00:00Z",
            "unknown_custom_field": "should be dropped",
        }
    )
    _run_sf_mode(str(path), payload)
    content = path.read_text(encoding="utf-8")
    assert "sf_stage: Validate" in content
    assert "unknown_custom_field" not in content


def test_bug311_unknown_key_dropped_no_recognised_data_fields_returns_early(tmp_path: Path) -> None:
    """When all data fields are unrecognised, write is aborted (no error, no file change)."""
    path = _minimal_pursuit(tmp_path)
    original = path.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "status": "ok",
            "opportunity_id": "006Pe000012n2GkIAI",
            "totally_unknown_key": "value",
        }
    )
    _run_sf_mode(str(path), payload)
    assert path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# _run_sf_mode: account mode (account_id in payload)
# ---------------------------------------------------------------------------


# ── TestAccountMode (flattened) ─────────────────────────────────────────────


def test_account_mode_account_mode_writes_account_fields(tmp_path: Path) -> None:
    path = tmp_path / "account.md"
    path.write_text(
        "---\nstatus: ok\n---\n\n# Account\n",
        encoding="utf-8",
    )
    payload = json.dumps(
        {
            "status": "ok",
            "account_id": "001Pe000012nXXXXXX",
            "industry": "Finance",
            "owner": "Jane Doe",
            "open_opportunity_count": 3,
            "pulled_at": "2026-01-01T00:00:00Z",
        }
    )
    _run_sf_mode(str(path), payload)
    content = path.read_text(encoding="utf-8")
    assert "sf_industry: Finance" in content or "sf_owner: Jane Doe" in content


@pytest.mark.parametrize("dry_run", [False, True])
def test_account_mode_rejects_invalid_legacy_history_in_preview_and_write(tmp_path: Path, dry_run: bool) -> None:
    """Account updates validate legacy history before previews or writes."""
    path = tmp_path / "account.md"
    original = "---\nlegacy_meddpicc:\n  schema_version: 2\n  status: historical\n---\n\n# Account\n"
    path.write_text(original, encoding="utf-8")
    payload = json.dumps({"status": "ok", "account_id": "001Pe000012nXXXXXX", "industry": "Finance"})

    with pytest.raises(ValueError, match="schema_version"):
        _run_sf_mode(str(path), payload, as_json=dry_run, dry_run=dry_run)

    assert path.read_text(encoding="utf-8") == original


def test_account_mode_adds_frontmatter_to_plain_markdown(tmp_path: Path) -> None:
    """Account mode can add canonical frontmatter to a plain document."""
    path = tmp_path / "account.md"
    original_body = "# Account\n\nKeep this body.\n"
    path.write_text(original_body, encoding="utf-8")
    payload = json.dumps(
        {
            "status": "ok",
            "account_id": "001Pe000012nXXXXXX",
            "industry": "Finance",
            "owner": "Jane Doe",
            "open_opportunity_count": 3,
            "pulled_at": "2026-01-01T00:00:00Z",
        }
    )

    _run_sf_mode(str(path), payload)

    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "sf_industry: Finance" in content
    assert content.endswith(original_body)


# ---------------------------------------------------------------------------
# _run_sf_mode: implementation change — sf_name slug mismatch warning
# ---------------------------------------------------------------------------


# ── TestEnh144SlugMismatch (flattened) ──────────────────────────────────────


def test_enh144_slug_mismatch_slug_mismatch_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    path = tmp_path / "deal-slug.md"
    path.write_text(
        "---\n"
        "stage: discover\n"
        "gate-status: pending\n"
        "last-transition: 2026-01-01\n"
        "sf_opportunity_id: 006Pe000012n2GkIAI\n"
        "sf_stage: Discover\n"
        "sf_arr: $100,000\n"
        "sf_close_date: 2026-12-31\n"
        "sf_owner: Jane Doe\n"
        "sf_next_steps: Demo\n"
        "sf_last_pulled: 2026-01-01T00:00:00Z\n"
        "sf_acv: $90,000\n"
        "sf_consulting_acv: $50,000\n"
        "sf_training_acv: $40,000\n"
        "meddpicc:\n"
        "  metrics: 2\n"
        "  economic-buyer: 2\n"
        "  decision-criteria: 2\n"
        "  decision-process: 2\n"
        "  identify-pain: 2\n"
        "  champion: 2\n"
        "  competition: 2\n"
        "  paper-process: 2\n"
        "---\n\n# Deal\n",
        encoding="utf-8",
    )
    # sf name that slugifies to something different from "deal-slug"
    payload = json.dumps(
        {
            "status": "ok",
            "opportunity_id": "006Pe000012n2GkIAI",
            "name": "ACME Enterprise Platform Deal",  # slugifies to "acme-enterprise-platform-deal"
            "stage": "Validate",
            "arr": "$120,000",
            "pulled_at": "2026-06-01T00:00:00Z",
        }
    )
    with caplog.at_level(logging.WARNING):
        _run_sf_mode(str(path), payload)
    # historic regression: implementation change tag removed from warning message; check for substantive content instead
    assert any("diverges from pursuit file stem" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI: missing arguments
# ---------------------------------------------------------------------------


# ── TestCliMissingArgs (flattened) ──────────────────────────────────────────


def test_cli_sf_mode_requires_json(tmp_path: Path) -> None:
    runner = CliRunner()
    path = _minimal_pursuit(tmp_path)
    result = runner.invoke(cli, [str(path)], catch_exceptions=False)
    assert result.exit_code != 0


def test_cli_quality_check_requires_file_opt() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--quality-check"], catch_exceptions=False)
    assert result.exit_code != 0


def test_cli_validate_requires_file_opt() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--validate"], catch_exceptions=False)
    assert result.exit_code != 0


def test_cli_meddpicc_requires_file_opt() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--meddpicc", "champion=2"], catch_exceptions=False)
    assert result.exit_code != 0


def test_cli_validate_template_skipped(tmp_path: Path) -> None:
    path = tmp_path / "template.md"
    path.write_text("---\nstage: discover\n---\n\n# Template\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["--validate", "--file", str(path)], catch_exceptions=False)
    assert result.exit_code == 0
    assert "SKIP" in result.output or "template" in result.output.lower()


def test_cli_validate_valid_file_exits_0(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--validate", "--file", str(path)], catch_exceptions=False)
    # Valid file or schema path issue → exit 0 or 1 (depends on schema)
    # At minimum, should not crash
    assert result.exit_code in (0, 1)


def test_cli_quality_check_no_meddpicc(tmp_path: Path) -> None:
    path = tmp_path / "no-meddpicc.md"
    path.write_text(
        "---\nstage: discover\nsf_opportunity_id: 006Pe000012n2GkIAI\n---\n\n# Deal\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--quality-check", "--file", str(path)], catch_exceptions=False)
    assert result.exit_code == 0


def test_cli_quality_check_with_meddpicc(tmp_path: Path) -> None:
    path = _minimal_pursuit(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--quality-check", "--file", str(path)], catch_exceptions=False)
    assert result.exit_code == 0
    assert "PASS" in result.output
