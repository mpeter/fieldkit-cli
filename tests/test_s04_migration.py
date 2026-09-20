"""Migration verification tests for S04: lib.io.load_pursuit() integration.

Confirms that the three internal sf_pipeline readers (_extract_opp_id,
reconcile.cli, and _quality_check_pursuit) call lib.io.load_pursuit()
as their primary frontmatter read path after the S04 migration.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.integration

# Ensure project root on path so imports resolve
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the modules under test
import fieldkit.commands.pursuit.audit as audit_mod  # noqa: E402
import fieldkit.commands.sf.frontmatter as frontmatter_mod  # noqa: E402
import fieldkit.commands.sf.reconcile as reconcile_mod  # noqa: E402
import fieldkit.commands.sf.sync as sync_mod  # noqa: E402


def _make_pursuit_with_stage(tmp_path: Path, extra_fm: str = "") -> Path:
    """Create a minimal valid pursuit file that load_pursuit() can parse."""
    content = f"---\nstage: discover\n{extra_fm}---\n# Test Pursuit\n\nBody content here.\n"
    p = tmp_path / "pursuit.md"
    p.write_text(content, encoding="utf-8")
    return p


# ── TestExtractOppIdUsesLibIo (flattened) ───────────────────────────────────


def test_extract_opp_id_uses_lib_io_extract_opp_id_uses_lib_io(tmp_path):
    """load_pursuit is called and the opp ID is returned from its result."""
    valid_id = "006Dn000001AbCdEFG"  # 18 alphanumeric chars (valid SF ID format)
    pursuit = _make_pursuit_with_stage(tmp_path, extra_fm=f"sf_opportunity_id: {valid_id}\n")

    # Patch load_pursuit where it was imported into sync
    with patch("fieldkit.commands.sf.sync.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.sf_opportunity_id = valid_id
        mock_load.return_value = (mock_model, "# Body\n", 1234567890.0)

        result = sync_mod._extract_opp_id(pursuit)

    mock_load.assert_called_once_with(pursuit)
    assert result == valid_id


def test_extract_opp_id_uses_lib_io_extract_opp_id_returns_none_when_empty(tmp_path):
    """load_pursuit is still called even when sf_opportunity_id is empty."""
    pursuit = _make_pursuit_with_stage(tmp_path)

    with patch("fieldkit.commands.sf.sync.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.sf_opportunity_id = None
        mock_load.return_value = (mock_model, "# Body\n", 1234567890.0)

        result = sync_mod._extract_opp_id(pursuit)

    mock_load.assert_called_once()
    assert result is None


def test_extract_opp_id_uses_lib_io_extract_opp_id_real_file_roundtrip(tmp_path):
    """End-to-end: real load_pursuit reads sf_opportunity_id from a valid file."""
    valid_id = "006Dn000001RealABC"  # 18 alphanumeric chars
    pursuit = _make_pursuit_with_stage(tmp_path, extra_fm=f'sf_opportunity_id: "{valid_id}"\n')
    result = sync_mod._extract_opp_id(pursuit)
    assert result == valid_id


def test_extract_opp_id_uses_lib_io_extract_opp_id_needs_lookup_returns_none(tmp_path, caplog):
    """historic regression: 'NEEDS-LOOKUP' is not in PLACEHOLDER_VALUES but fails OPP_ID_RE.

    The model path must validate raw_val against OPP_ID_RE and return None
    (with a WARNING) for any string that is not a 15- or 18-char alphanumeric
    Salesforce ID — including informal placeholder strings like 'NEEDS-LOOKUP'.
    """
    import logging

    with patch("fieldkit.commands.sf.sync.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.sf_opportunity_id = "NEEDS-LOOKUP"
        mock_load.return_value = (mock_model, "# Body\n", 1234567890.0)

        with caplog.at_level(logging.WARNING, logger="fieldkit.commands.sf.sync"):
            result = sync_mod._extract_opp_id(tmp_path / "pursuit.md")

    assert result is None, f"Expected None for 'NEEDS-LOOKUP', got {result!r}"
    assert any("looks invalid" in r.message and "NEEDS-LOOKUP" in r.message for r in caplog.records), (
        f"Expected WARNING about invalid ID, got: {[r.message for r in caplog.records]}"
    )


def test_extract_opp_id_uses_lib_io_extract_opp_id_valid_18char_id_returned(tmp_path):
    """historic regression regression guard: a valid 18-char SF ID passes OPP_ID_RE and is returned."""
    valid_id = "006Dn000001AbCdEF0"  # exactly 18 alphanumeric chars
    with patch("fieldkit.commands.sf.sync.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.sf_opportunity_id = valid_id
        mock_load.return_value = (mock_model, "# Body\n", 1234567890.0)

        result = sync_mod._extract_opp_id(tmp_path / "pursuit.md")

    assert result == valid_id, f"Expected valid ID to be returned unchanged, got {result!r}"


# ── TestReconcileMainReadsViaLibIo (flattened) ──────────────────────────────


def test_reconcile_main_reads_via_lib_io_reconcile_main_reads_via_lib_io(tmp_path):
    """load_pursuit is called when reconcile.cli processes a pursuit file."""
    # Use pursuit_full.md fixture which has sf_stage, sf_close_date, sf_arr
    import shutil

    fixture = ROOT / "tests" / "fixtures" / "pursuit_full.md"
    pursuit = tmp_path / "pursuit_full.md"
    shutil.copy(fixture, pursuit)

    with patch("fieldkit.commands.sf.reconcile.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.sf_stage = "Propose"
        mock_model.sf_close_date = "2026-06-30"
        mock_model.sf_arr = "100000"
        mock_load.return_value = (mock_model, "", 1234567890.0)

        CliRunner().invoke(reconcile_mod.cli, [str(pursuit)])

    mock_load.assert_called_once_with(str(pursuit))
    assert mock_load.call_count == 1


def test_reconcile_main_reads_via_lib_io_reconcile_main_real_file_no_crash(tmp_path):
    """End-to-end: reconcile.cli runs without error on a valid pursuit file."""
    import shutil

    fixture = ROOT / "tests" / "fixtures" / "pursuit_full.md"
    pursuit = tmp_path / "pursuit_full.md"
    shutil.copy(fixture, pursuit)

    result = CliRunner().invoke(reconcile_mod.cli, [str(pursuit)])
    assert result.exit_code in (0, None)


# ── TestQualityCheckBackstoryScanViaLibIo (flattened) ───────────────────────


def test_quality_check_backstory_scan_via_lib_io_quality_check_uses_lib_io(tmp_path):
    """load_pursuit is called during _quality_check_pursuit()."""
    pursuit = _make_pursuit_with_stage(tmp_path)

    with patch("fieldkit.commands.sf.frontmatter.load_pursuit") as mock_load:
        mock_model = MagicMock()
        mock_model.model_dump.return_value = {}
        mock_load.return_value = (mock_model, "", 1234567890.0)

        frontmatter_mod._quality_check_pursuit(str(pursuit))

    mock_load.assert_called_once_with(str(pursuit))
    assert mock_load.call_count == 1


def test_quality_check_backstory_scan_via_lib_io_backstory_in_frontmatter_flagged_via_lib_io(tmp_path, capsys):
    """Backstory prohibition scan fires when load_pursuit returns flagged data."""
    pursuit = _make_pursuit_with_stage(tmp_path, extra_fm='champion_name: "[Backstory] Jane Smith"\n')

    with patch("fieldkit.commands.sf.frontmatter.load_pursuit") as mock_load:
        mock_model = MagicMock()
        # Simulate model_dump returning a dict with a backstory string
        mock_model.model_dump.return_value = {"champion_name": "[Backstory] Jane Smith"}
        mock_load.return_value = (mock_model, "", 1234567890.0)

        frontmatter_mod._quality_check_pursuit(str(pursuit))

    err = capsys.readouterr().err
    assert "Backstory-derived data found" in err


def test_quality_check_backstory_scan_via_lib_io_quality_check_real_file_with_backstory(tmp_path, capsys):
    """End-to-end: _quality_check_pursuit warns on [Backstory] in sf_next_steps.

    Uses sf_next_steps (a known schema field in PursuitFrontmatter) so that
    load_pursuit() succeeds and model_dump() includes the flagged string.
    """
    content = (
        '---\nstage: discover\nsf_next_steps: "[Backstory] schedule follow-up via People.AI signal"\n---\n# Test\n'
    )
    p = tmp_path / "pursuit.md"
    p.write_text(content, encoding="utf-8")

    frontmatter_mod._quality_check_pursuit(str(p))
    err = capsys.readouterr().err
    assert "Backstory-derived data found" in err


def test_check_backstory_returns_frontmatter_finding_with_severity_and_message() -> None:
    findings = audit_mod._check_backstory({"champion_name": "[Backstory] Jane Smith"}, "")

    assert len(findings) == 1
    assert findings[0].level == "ERROR"
    assert "Field `champion_name` contains Backstory-derived data" in findings[0].message


@pytest.mark.parametrize(
    ("body", "level", "message"),
    [
        (
            "Notes (via Backstory) were imported.",
            "WARNING",
            "Body contains '(via Backstory)' attribution — remove or rephrase",
        ),
        (
            "## Backstory Activity\n\nImported notes.",
            "WARNING",
            "Body contains 'Backstory Activity' section header — remove",
        ),
        (
            "Engagement Score: 87",
            "WARNING",
            "Body contains 'Engagement Score' metric — remove",
        ),
    ],
)
def test_check_backstory_returns_body_finding_with_severity_and_message(body: str, level: str, message: str) -> None:
    findings = audit_mod._check_backstory({}, body)

    assert len(findings) == 1
    assert findings[0].level == level
    assert findings[0].message == message
