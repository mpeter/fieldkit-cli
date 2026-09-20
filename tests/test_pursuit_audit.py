"""Tests for fieldkit.pursuit.audit — pursuit frontmatter validation."""

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from fieldkit.commands.pursuit.audit import (
    _VALID_GATE_RESULTS,
    _check_transition_history,
    _parse_sf_date,
    apply_fixes,
    audit_directory,
    audit_file,
    check_yaml_duplicates,
    check_yaml_duplicates_directory,
)
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit.gate_criteria import ALLOWED_GATE_STATUSES
from fieldkit.pursuit.io import parse_frontmatter_fallback

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_FRONTMATTER = """\
---
stage: discover
gate-status: pending
last-transition: 2026-05-10
transition-history:
  - date: 2026-05-10
    from: ""
    to: discover
    gate-result: pass
    override-reason: ""
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
sf_next_steps: Schedule discovery call
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit Title

Body text here.
"""


def _make_file(tmp_path: Path, content: str, name: str = "test-pursuit.md") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_valid(tmp_path: Path) -> None:
    fm, body = parse_frontmatter_fallback(VALID_FRONTMATTER)
    assert fm is not None
    assert fm["stage"] == "discover"
    assert "# Pursuit Title" in body


def test_parse_frontmatter_no_delimiters(tmp_path: Path) -> None:
    fm, _body = parse_frontmatter_fallback("# Just markdown\nNo frontmatter.")
    assert fm is None


def test_parse_frontmatter_empty_block(tmp_path: Path) -> None:
    content = "---\n---\n\n# Body"
    fm, _body = parse_frontmatter_fallback(content)
    assert fm == {}


# ---------------------------------------------------------------------------
# _parse_sf_date
# ---------------------------------------------------------------------------


def test_parse_sf_date_slash_format() -> None:
    d = _parse_sf_date("6/30/2027")
    assert d == date(2027, 6, 30)


def test_parse_sf_date_iso_format() -> None:
    d = _parse_sf_date("2027-06-30")
    assert d == date(2027, 6, 30)


def test_parse_sf_date_yaml_date_object() -> None:
    parsed = _parse_sf_date(date(2027, 6, 30))
    assert parsed == date(2027, 6, 30)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (datetime(2027, 6, 30, 12, 30), date(2027, 6, 30)),
        ("   ", None),
        ("2/30/2027", None),
    ],
)
def test_parse_sf_date_safe_loaded_timestamp_and_invalid_values(raw: object, expected: date | None) -> None:
    """Accept YAML timestamps while safely rejecting blank and impossible dates."""
    parsed = _parse_sf_date(raw)

    assert parsed == expected


def test_parse_sf_date_empty() -> None:
    assert _parse_sf_date("") is None
    assert _parse_sf_date(None) is None


def test_parse_sf_date_invalid() -> None:
    assert _parse_sf_date("not-a-date") is None


# ---------------------------------------------------------------------------
# audit_file — structure checks
# ---------------------------------------------------------------------------


def test_audit_file_valid(tmp_path: Path) -> None:
    p = _make_file(tmp_path, VALID_FRONTMATTER)
    result = audit_file(p, today=date(2026, 6, 1))
    assert result.category == "COMPLIANT"
    assert result.qualification_status == "unavailable"


def test_apply_fixes_dry_run_reports_changes_without_mutating_file(tmp_path: Path) -> None:
    from fieldkit.commands.pursuit.audit import apply_fixes

    path = tmp_path / "deal.md"
    original = "---\nsf-opportunity-id: 006xx\nsf-opportunity-number: legacy\n---\nBody\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat().st_mtime_ns

    result = apply_fixes(path, dry_run=True)

    assert result.renames == 1
    assert result.legacy_removed == 1
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before


def test_audit_file_populates_stage(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("stage: discover", "stage: closed-won")
    p = _make_file(tmp_path, content)
    result = audit_file(p, today=date(2026, 6, 1))
    assert result.stage == "closed-won"


def test_audit_file_missing_stage(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("stage: discover\n", "")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    errors = [f.message for f in result.errors]
    assert any("stage" in e for e in errors)


def test_audit_file_invalid_stage(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("stage: discover", "stage: unknown-stage")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert any("stage" in f.message for f in result.errors)


def test_audit_file_invalid_gate_status(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("gate-status: pending", "gate-status: approved")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    errors = [f for f in result.errors if "gate-status" in f.message]
    assert errors, "Expected a gate-status validation error for 'approved'"
    # Error message must reference the allowed set so callers know what values are valid
    assert any(str(sorted(ALLOWED_GATE_STATUSES)) in f.message for f in errors)


def test_audit_file_valid_gate_statuses(tmp_path: Path) -> None:
    """Each value in ALLOWED_GATE_STATUSES must produce no gate-status errors."""
    for status in ALLOWED_GATE_STATUSES:
        content = VALID_FRONTMATTER.replace("gate-status: pending", f"gate-status: {status}")
        p = tmp_path / f"pursuit-{status}.md"
        p.write_text(content, encoding="utf-8")
        result = audit_file(p)
        gate_errors = [f for f in result.errors if "gate-status" in f.message]
        assert gate_errors == [], f"Unexpected gate-status errors for valid status '{status}': {gate_errors}"


def test_audit_file_no_frontmatter(tmp_path: Path) -> None:
    p = _make_file(tmp_path, "# No frontmatter\nJust content.")
    result = audit_file(p)
    assert result.category == "ERROR"
    assert result.parse_error is not None


# ---------------------------------------------------------------------------
# audit_file — SF naming checks
# ---------------------------------------------------------------------------


def test_audit_file_hyphenated_sf_opportunity_id(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("sf_opportunity_id:", "sf-opportunity-id:")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert any("sf-opportunity-id" in f.message for f in result.errors)


def test_audit_file_hyphenated_sf_stage_warning(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("sf_stage:", "sf-stage:")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert any("sf-stage" in f.message for f in result.warnings)


# ---------------------------------------------------------------------------
# audit_file — MEDDPICC checks
# ---------------------------------------------------------------------------


def test_audit_file_legacy_meddpicc_missing_element_is_not_current_error(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("  champion: 2\n", "")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert not any("champion" in f.message for f in result.errors)
    assert result.qualification_status == "unavailable"


def test_audit_file_legacy_meddpicc_value_is_not_revalidated_as_current(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("  champion: 2", "  champion: 5")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert not any("champion" in f.message or "0-3" in f.message for f in result.errors)


@pytest.mark.parametrize(
    "historical_yaml",
    [
        "legacy_meddpicc:\n  schema_version: 2\n  status: current",
        "legacy_meddpicc:\n  schema_version: true\n  status: historical",
        "legacy_meddpicc: invalid",
        "meddpicc:\n  champion: 2\nlegacy_meddpicc:\n  schema_version: 1\n  status: historical",
        "meddpicc:\n  schema_version: 1\n  champion: 2",
    ],
    ids=("invalid-envelope", "boolean-version", "non-mapping", "both-keys", "reserved-former-metadata"),
)
def test_audit_file_rejects_invalid_historical_qualification(tmp_path: Path, historical_yaml: str) -> None:
    """Report malformed historical envelopes instead of calling the pursuit compliant."""
    content = VALID_FRONTMATTER.replace(
        "meddpicc:\n  metrics: 2\n  economic-buyer: 2\n  decision-criteria: 2\n  decision-process: 2\n"
        "  identify-pain: 2\n  champion: 2\n  competition: 2\n  paper-process: 2\n",
        f"{historical_yaml}\n",
    )

    result = audit_file(_make_file(tmp_path, content))

    assert any("Invalid historical qualification" in finding.message for finding in result.errors)


def test_audit_file_accepts_irregular_historical_values(tmp_path: Path) -> None:
    """Validate the envelope without restoring numeric score interpretation."""
    content = VALID_FRONTMATTER.replace("  champion: 2", "  champion: not-scored")

    result = audit_file(_make_file(tmp_path, content))

    assert not any("Invalid historical qualification" in finding.message for finding in result.errors)


def test_audit_file_legacy_elements_do_not_create_qualification_critical(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("  champion: 2", "  champion: 0").replace(
        "  economic-buyer: 2", "  economic-buyer: 1"
    )
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert not any("conversation, not a deal" in f.message for f in result.criticals)


def test_audit_file_legacy_composite_does_not_create_threshold_warning(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("  metrics: 2", "  metrics: 0")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert not any("qualification threshold" in f.message for f in result.warnings)


def test_audit_file_legacy_zero_does_not_create_gap_warning(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("  champion: 2", "  champion: 0")
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert not any("MEDDPICC" in f.message for f in result.warnings)


# ---------------------------------------------------------------------------
# audit_file — close date checks
# ---------------------------------------------------------------------------


def test_audit_file_overdue_close_date(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("sf_close_date: 12/31/2027", "sf_close_date: 1/1/2025")
    p = _make_file(tmp_path, content)
    result = audit_file(p, today=date(2026, 6, 1))
    assert any("overdue" in f.message for f in result.errors)


def test_audit_file_invalid_close_date_skips_date_findings(tmp_path: Path) -> None:
    """Do not derive deadline findings from an unparseable close date."""
    content = VALID_FRONTMATTER.replace("sf_close_date: 12/31/2027", "sf_close_date: not-a-date")
    p = _make_file(tmp_path, content)

    result = audit_file(p, today=date(2026, 6, 1))

    assert not any("close date" in finding.message.lower() for finding in result.findings)


def test_audit_file_closed_stage_skips_date_check(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("stage: discover", "stage: closed-won").replace(
        "sf_close_date: 12/31/2027", "sf_close_date: 1/1/2025"
    )
    p = _make_file(tmp_path, content)
    result = audit_file(p, today=date(2026, 6, 1))
    assert not any("overdue" in f.message for f in result.errors)


def test_audit_file_approaching_close_ignores_legacy_paper_process(tmp_path: Path) -> None:
    today = date(2026, 6, 1)
    # Close date 7 days out, paper-process=0
    content = VALID_FRONTMATTER.replace("sf_close_date: 12/31/2027", "sf_close_date: 6/8/2026").replace(
        "  paper-process: 2", "  paper-process: 0"
    )
    p = _make_file(tmp_path, content)
    result = audit_file(p, today=today)
    assert not any("paper-process" in f.message for f in result.warnings)
    assert any("SF stage" in f.message for f in result.warnings)


# ---------------------------------------------------------------------------
# audit_file — backstory checks
# ---------------------------------------------------------------------------


def test_audit_file_backstory_in_frontmatter(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace(
        "sf_next_steps: Schedule discovery call", 'sf_next_steps: "[Backstory] via signals"'
    )
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert any("Backstory" in f.message for f in result.errors)


def test_audit_file_backstory_in_body(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER + "\nSome note (via Backstory) here.\n"
    p = _make_file(tmp_path, content)
    result = audit_file(p)
    assert any("via Backstory" in f.message for f in result.warnings)


# ---------------------------------------------------------------------------
# apply_fixes
# ---------------------------------------------------------------------------


def test_apply_fixes_renames_hyphenated(tmp_path: Path) -> None:
    content = VALID_FRONTMATTER.replace("sf_opportunity_id: ", "sf-opportunity-id: ").replace("sf_stage:", "sf-stage:")
    p = _make_file(tmp_path, content)
    fix_result = apply_fixes(p)
    assert fix_result.renames >= 2
    new_content = p.read_text(encoding="utf-8")
    assert "sf-opportunity-id:" not in new_content
    assert "sf_opportunity_id:" in new_content
    assert "sf-stage:" not in new_content
    assert "sf_stage:" in new_content


@pytest.mark.parametrize(
    "fields",
    [
        "sf_opportunity_id: 006CANONICAL\nsf-opportunity-id: 006LEGACY",
        "sf-opportunity-id: 006LEGACY\nsf_opportunity_id: 006CANONICAL",
    ],
)
def test_apply_fixes_refuses_conflicting_canonical_and_legacy_fields(tmp_path: Path, fields: str) -> None:
    original = f"---\nstage: discover\n{fields}\n---\nBody\n"
    path = _make_file(tmp_path, original)

    with pytest.raises(ValueError, match=r"conflicting.*sf-opportunity-id.*sf_opportunity_id"):
        apply_fixes(path)

    assert path.read_text(encoding="utf-8") == original


def test_apply_fixes_collapses_equal_canonical_and_legacy_fields(tmp_path: Path) -> None:
    path = _make_file(
        tmp_path,
        "---\nstage: discover\nsf_opportunity_id: 006SAME\nsf-opportunity-id: 006SAME\n---\nBody\n",
    )

    result = apply_fixes(path)
    frontmatter, _ = parse_frontmatter_fallback(path.read_text(encoding="utf-8"))

    assert result.renames == 1
    assert frontmatter["sf_opportunity_id"] == "006SAME"
    assert "sf-opportunity-id" not in frontmatter


def test_apply_fixes_refuses_write_when_file_changes_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _make_file(tmp_path, "---\nstage: discover\nsf-opportunity-id: 006LEGACY\n---\nBody\n")
    original_read_text = Path.read_text
    original = original_read_text(path, encoding="utf-8")
    concurrent = original + "Concurrent note\n"

    def mutate_then_return_original(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            self.write_text(concurrent, encoding="utf-8")
            current_ns = self.stat().st_mtime_ns
            os.utime(self, ns=(current_ns + 1_000_000_000, current_ns + 1_000_000_000))
            return original
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mutate_then_return_original)

    with pytest.raises(FrontmatterStalenessError, match="modified since last read"):
        apply_fixes(path)

    assert original_read_text(path, encoding="utf-8") == concurrent


def test_apply_fixes_removes_legacy_hyphen_form(tmp_path: Path) -> None:
    """sf-opportunity-number (hyphen form) must be stripped — it is legacy per R05."""
    content = VALID_FRONTMATTER.replace(
        "---\n\n# Pursuit Title", "sf-opportunity-number: OP-1234\n---\n\n# Pursuit Title"
    )
    p = _make_file(tmp_path, content)
    fix_result = apply_fixes(p)
    assert fix_result.legacy_removed >= 1
    assert "sf-opportunity-number" not in p.read_text(encoding="utf-8")


def test_apply_fixes_does_not_strip_underscore_opportunity_number(tmp_path: Path) -> None:
    """sf_opportunity_number (underscore form) is now canonical — apply_fixes must NOT remove it.

    Regression guard for implementation change: reversing the LEGACY_REMOVE entry so audit --fix
    no longer strips the field that SF pipeline writes.
    """
    content = VALID_FRONTMATTER.replace(
        "meddpicc:\n",
        "legacy_meddpicc:\n  schema_version: 1\n  status: historical\n",
    ).replace("---\n\n# Pursuit Title", "sf_opportunity_number: 71721820\n---\n\n# Pursuit Title")
    p = _make_file(tmp_path, content)
    fix_result = apply_fixes(p)
    # No legacy removals should occur for the underscore form
    assert fix_result.legacy_removed == 0
    # Field must still be present in the file
    assert "sf_opportunity_number: 71721820" in p.read_text(encoding="utf-8")


def test_apply_fixes_canonicalizes_former_meddpicc_in_same_atomic_write(tmp_path: Path) -> None:
    """Any authorized audit repair also contains former score data as historical evidence."""
    original = VALID_FRONTMATTER.replace("sf_opportunity_id:", "sf-opportunity-id:")
    p = _make_file(tmp_path, original)

    fix_result = apply_fixes(p)

    frontmatter, _ = parse_frontmatter_fallback(p.read_text(encoding="utf-8"))
    assert fix_result.renames >= 1
    assert "meddpicc" not in frontmatter
    assert frontmatter["legacy_meddpicc"]["schema_version"] == 1
    assert frontmatter["legacy_meddpicc"]["status"] == "historical"
    assert frontmatter["legacy_meddpicc"]["champion"] == 2


def test_apply_fixes_no_changes_needed(tmp_path: Path) -> None:
    canonical = VALID_FRONTMATTER.replace(
        "meddpicc:\n",
        "legacy_meddpicc:\n  schema_version: 1\n  status: historical\n",
    )
    p = _make_file(tmp_path, canonical)
    fix_result = apply_fixes(p)
    assert fix_result.total_changes == 0


# ---------------------------------------------------------------------------
# audit_directory
# ---------------------------------------------------------------------------


def test_audit_directory_scans_pursuits(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "deal-1.md").write_text(VALID_FRONTMATTER, encoding="utf-8")
    # gmail-intel.md should be excluded
    (accounts / "gmail-intel.md").write_text(VALID_FRONTMATTER, encoding="utf-8")

    results = audit_directory(tmp_path, today=date(2026, 6, 1))
    assert len(results) == 1
    assert results[0].relative_path == "acme/pursuits/deal-1.md"


def test_audit_directory_account_filter(tmp_path: Path) -> None:
    for account in ("acme", "globex"):
        p = tmp_path / "accounts" / account / "pursuits"
        p.mkdir(parents=True)
        (p / "deal.md").write_text(VALID_FRONTMATTER, encoding="utf-8")

    results = audit_directory(tmp_path, account_filter="acme", today=date(2026, 6, 1))
    assert len(results) == 1
    assert "acme" in results[0].relative_path


def test_audit_directory_empty_accounts(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    results = audit_directory(tmp_path)
    assert results == []


def test_audit_directory_no_accounts_dir(tmp_path: Path) -> None:
    results = audit_directory(tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# check_yaml_duplicates
# ---------------------------------------------------------------------------

_DUPLICATE_KEY_FRONTMATTER = """\
---
stage: qualify
gate-status: pending
last-transition: "2026-01-01"
transition-history: []
meddpicc:
  metrics: 1
  economic-buyer: 1
  decision-criteria: 1
  decision-process: 1
  identify-pain: 1
  champion: 1
  competition: 1
  paper-process: 1
sf_opportunity_id: OPP-DUP
sf_opportunity_id: OPP-DUP-AGAIN
---
body
"""

_CLEAN_FRONTMATTER = VALID_FRONTMATTER


def test_check_yaml_duplicates_detects_duplicates(tmp_path: Path) -> None:
    p = tmp_path / "deal.md"
    p.write_text(_DUPLICATE_KEY_FRONTMATTER, encoding="utf-8")
    result = check_yaml_duplicates(p)
    assert result.findings, "Expected findings for duplicate key"
    messages = [f.message for f in result.findings]
    assert any("sf_opportunity_id" in m for m in messages)


def test_check_yaml_duplicates_clean_file(tmp_path: Path) -> None:
    p = tmp_path / "deal.md"
    p.write_text(_CLEAN_FRONTMATTER, encoding="utf-8")
    result = check_yaml_duplicates(p)
    assert not result.findings


def test_audit_file_pre_pipeline_skips_date_check(tmp_path: Path) -> None:
    path = _make_file(
        tmp_path,
        "---\nstage: pre-pipeline\nsf_close_date: 2020-01-01\nsf_stage: Prospecting\n---\n",
    )

    result = audit_file(path, today=date(2026, 6, 1))

    assert not any("close date" in finding.message.lower() for finding in result.findings)


def test_audit_file_reports_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "unreadable.md"
    path.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    def raise_oserror(*_args: object, **_kwargs: object) -> str:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", raise_oserror)
    result = audit_file(path, today=date(2026, 6, 1))

    assert result.parse_error == "Cannot read file: simulated read failure"


def test_check_yaml_duplicates_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "deal.md"
    p.write_text("no frontmatter here\n", encoding="utf-8")
    result = check_yaml_duplicates(p)
    assert result.parse_error is not None


def test_check_yaml_duplicates_directory_finds_duplicates(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "dup.md").write_text(_DUPLICATE_KEY_FRONTMATTER, encoding="utf-8")
    (accounts / "clean.md").write_text(_CLEAN_FRONTMATTER, encoding="utf-8")

    results = check_yaml_duplicates_directory(tmp_path)
    # Only the file with duplicates should be returned
    assert len(results) == 1
    assert "dup.md" in results[0].relative_path


def test_check_yaml_duplicates_directory_all_clean(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "clean.md").write_text(_CLEAN_FRONTMATTER, encoding="utf-8")

    results = check_yaml_duplicates_directory(tmp_path)
    assert results == []


def test_check_yaml_duplicates_directory_excludes_gmail_intel(tmp_path: Path) -> None:
    accounts = tmp_path / "accounts" / "acme" / "pursuits"
    accounts.mkdir(parents=True)
    (accounts / "gmail-intel.md").write_text(_DUPLICATE_KEY_FRONTMATTER, encoding="utf-8")

    results = check_yaml_duplicates_directory(tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# _check_transition_history / audit_file — gate-result validation (historic regression, historic regression)
# ---------------------------------------------------------------------------

_FRONTMATTER_WITH_INVALID_GATE_RESULT = """\
---
stage: discover
gate-status: pending
last-transition: 2026-05-10
transition-history:
  - date: 2026-05-10
    from: ""
    to: discover
    gate-result: closed
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
sf_next_steps: Schedule discovery call
sf_last_pulled: 2026-05-14T17:35:36Z
---

# Pursuit Title
"""


def test_check_transition_history_invalid_gate_result_produces_warning() -> None:
    """gate-result: closed is not in _VALID_GATE_RESULTS — must produce a WARNING."""
    fm = {"transition-history": [{"date": "2026-05-10", "from": "", "to": "discover", "gate-result": "closed"}]}
    findings = _check_transition_history(fm)
    assert len(findings) == 1
    assert findings[0].level == "WARNING"
    assert "closed" in findings[0].message
    assert "gate-result" in findings[0].message
    assert str(sorted(_VALID_GATE_RESULTS)) in findings[0].message


def test_check_transition_history_valid_gate_result_no_finding() -> None:
    """gate-result: pass is valid — must produce no findings."""
    fm = {"transition-history": [{"date": "2026-05-10", "from": "", "to": "discover", "gate-result": "pass"}]}
    findings = _check_transition_history(fm)
    assert findings == []


def test_check_transition_history_missing_gate_result_no_finding() -> None:
    """Entry without gate-result key must produce no findings."""
    fm = {"transition-history": [{"date": "2026-05-10", "from": "", "to": "discover"}]}
    findings = _check_transition_history(fm)
    assert findings == []


def test_check_transition_history_empty_list_no_finding() -> None:
    """Empty transition-history must produce no findings."""
    fm: dict[str, list[object]] = {"transition-history": []}
    findings = _check_transition_history(fm)
    assert findings == []


def test_audit_file_invalid_gate_result_in_history_produces_warning(tmp_path: Path) -> None:
    """End-to-end: audit_file surfaces WARNING for invalid gate-result in transition-history."""
    p = _make_file(tmp_path, _FRONTMATTER_WITH_INVALID_GATE_RESULT)
    result = audit_file(p, today=date(2026, 6, 1))
    gate_result_warnings = [f for f in result.warnings if "gate-result" in f.message]
    assert gate_result_warnings, "Expected a gate-result WARNING from audit_file"
    assert any("closed" in f.message for f in gate_result_warnings)


def test_audit_file_valid_gate_result_in_history_no_warning(tmp_path: Path) -> None:
    """End-to-end: audit_file produces no gate-result warning when value is valid."""
    p = _make_file(tmp_path, VALID_FRONTMATTER)  # VALID_FRONTMATTER uses gate-result: pass
    result = audit_file(p, today=date(2026, 6, 1))
    gate_result_warnings = [f for f in result.warnings if "gate-result" in f.message]
    assert gate_result_warnings == [], f"Unexpected gate-result warnings: {gate_result_warnings}"


def test_check_transition_history_manual_gate_result_produces_warning() -> None:
    """historic regression: gate-result: manual is not in _VALID_GATE_RESULTS — must produce a WARNING.

    Regression test for the specific value 'manual' found in a legacy pursuit.
    The finding message must mention both 'gate-result' and 'manual' so the
    operator can identify the field.
    """
    fm = {"transition-history": [{"date": "2026-05-10", "from": "qualify", "to": "discover", "gate-result": "manual"}]}
    findings = _check_transition_history(fm)
    assert len(findings) == 1, f"Expected exactly 1 finding, got {findings}"
    assert findings[0].level == "WARNING"
    assert "gate-result" in findings[0].message, f"Expected 'gate-result' in message: {findings[0].message}"
    assert "manual" in findings[0].message, f"Expected 'manual' in message: {findings[0].message}"


def test_audit_file_manual_gate_result_surfaces_to_output(tmp_path: Path) -> None:
    """historic regression: audit_file surfaces WARNING for gate-result: manual in transition-history.

    Verifies the end-to-end path: _check_transition_history() findings reach
    the AuditResult.warnings list that is rendered in the audit report.
    """
    content = VALID_FRONTMATTER.replace("gate-result: pass", "gate-result: manual")
    p = _make_file(tmp_path, content)
    result = audit_file(p, today=date(2026, 6, 1))
    gate_result_warnings = [f for f in result.warnings if "gate-result" in f.message and "manual" in f.message]
    assert gate_result_warnings, "Expected a WARNING mentioning 'gate-result' and 'manual' in audit_file output"


# ---------------------------------------------------------------------------
# historic regression: --account nonexistent must produce clear error, not generic message
# ---------------------------------------------------------------------------


# ── TestBug159AccountNotFound (flattened) ───────────────────────────────────


def test_bug159_account_not_found_nonexistent_account_exits_3_with_clear_message(tmp_path: Path) -> None:
    """When --account names a directory that does not exist, exit 3 with 'Account directory not found'."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    # Create a minimal accounts dir so the top-level check passes
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True)

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, ["--account", "nonexistent-account-xyz"])

    assert result.exit_code == 3, f"Expected exit code 3, got {result.exit_code}"
    output = (result.output or "") + (result.stderr if hasattr(result, "stderr") else "")
    assert "Account directory not found" in output or "not found" in output.lower(), (
        f"Expected clear 'Account directory not found' error, got:\n{output}"
    )
    assert "No pursuit files found" not in output, (
        "Must not show generic 'No pursuit files found' when account dir is missing"
    )


def test_bug159_account_not_found_existing_account_proceeds_normally(tmp_path: Path) -> None:
    """When --account names an existing directory, audit proceeds (no early exit)."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    # Create accounts/myaccount/pursuits/ with a valid pursuit file
    accounts_dir = tmp_path / "accounts"
    pursuit_dir = accounts_dir / "myaccount" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "test-pursuit.md").write_text(VALID_FRONTMATTER, encoding="utf-8")

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, ["--account", "myaccount"])

    # Should not exit 3 with "Account directory not found"
    output = result.output or ""
    assert "Account directory not found" not in output


# ---------------------------------------------------------------------------
# historic regression: warn before overwriting existing audit report file
# ---------------------------------------------------------------------------


# ── TestBug026OverwriteWarning (flattened) ──────────────────────────────────


def _bug026_overwrite_warning_setup_accounts(tmp_path: Path) -> None:
    """Create a minimal accounts tree with one valid pursuit file."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(VALID_FRONTMATTER, encoding="utf-8")


def test_bug026_overwrite_warning_warns_when_report_already_exists(tmp_path: Path) -> None:
    """When --output points to an existing file, a WARNING must appear on stderr."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    _bug026_overwrite_warning_setup_accounts(tmp_path)
    report_path = tmp_path / "report.md"
    # Pre-create the report file so it already exists
    report_path.write_text("old content", encoding="utf-8")

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, ["--output", str(report_path)])

    assert result.exit_code == 0, f"Unexpected exit code: {result.exit_code}\n{result.output}"
    # Warning must appear on stderr (Click 8.2+ always captures stderr separately)
    # implementation change: demoted from WARNING to INFO — check log output (CliRunner captures stdout)
    # The Overwriting message is now an INFO log, not a stderr WARNING
    assert "Overwriting existing report" not in result.output  # no longer on stderr
    # File must still be written — that's the key behavior under test
    # File must still be written (overwrite proceeds)
    assert report_path.read_text(encoding="utf-8") != "old content"


def test_bug026_overwrite_warning_no_warning_when_report_is_new(tmp_path: Path) -> None:
    """When the report path does not yet exist, no WARNING is emitted."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    _bug026_overwrite_warning_setup_accounts(tmp_path)
    report_path = tmp_path / "new-report.md"
    # Ensure the file does NOT exist
    assert not report_path.exists()

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, ["--output", str(report_path)])

    assert result.exit_code == 0, f"Unexpected exit code: {result.exit_code}\n{result.output}"
    # Click 8.2+ always captures stderr separately
    stderr = result.stderr
    assert "WARNING" not in stderr, f"Unexpected WARNING on stderr:\n{stderr}"
    assert report_path.exists(), "Report file must be created"


# ---------------------------------------------------------------------------
# Task 10.1 — audit_cmd CLI: exit codes and report file
# ---------------------------------------------------------------------------


def _setup_compliant_accounts(tmp_path: Path) -> None:
    """Create a minimal accounts tree with one fully-compliant pursuit file."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(VALID_FRONTMATTER, encoding="utf-8")


def _setup_violation_accounts(tmp_path: Path) -> None:
    """Create an accounts tree with a pursuit that has a frontmatter error."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    # Missing 'stage' field → ERROR category
    bad_content = VALID_FRONTMATTER.replace("stage: discover\n", "")
    (pursuit_dir / "bad-deal.md").write_text(bad_content, encoding="utf-8")


@pytest.mark.unit
def test_audit_exits_zero_all_compliant(tmp_path: Path) -> None:
    """audit_cmd exits 0 when all pursuit files are compliant."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    _setup_compliant_accounts(tmp_path)

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, [], catch_exceptions=False)

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}:\n{result.output}"


@pytest.mark.unit
def test_audit_exits_nonzero_violations(tmp_path: Path) -> None:
    """audit_cmd exits non-zero (3) when accounts directory is missing."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    # Point data root at a directory that has no accounts/ subdirectory
    empty_root = tmp_path / "empty"
    empty_root.mkdir()

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=empty_root):
        runner = CliRunner()
        result = runner.invoke(audit_cli, [])

    # Missing accounts dir → exit 3 (data error)
    assert result.exit_code != 0, "Expected non-zero exit when accounts dir is absent"


@pytest.mark.unit
def test_audit_writes_report_file(tmp_path: Path) -> None:
    """audit_cmd writes a Markdown report file to the specified --output path."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli

    _setup_compliant_accounts(tmp_path)
    report_path = tmp_path / "report.md"

    with patch("fieldkit.commands.pursuit.audit_cmd._data_root", return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(audit_cli, ["--output", str(report_path)], catch_exceptions=False)

    assert result.exit_code == 0, f"Unexpected exit {result.exit_code}:\n{result.output}"
    assert report_path.exists(), "Report file must be created"
    content = report_path.read_text(encoding="utf-8")
    assert "Pursuit Compliance Report" in content, "Report must contain the standard header"
