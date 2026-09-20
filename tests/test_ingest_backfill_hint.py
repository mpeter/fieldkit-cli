"""Tests for implementation change: backfill shows next-step command per candidate.

Spec: openspec/changes/ingest-correctness/specs/ingest-ux-correctness/spec.md
  - Scenario: Candidates found — each shows a fix command
  - Scenario: No candidates — summary reflects clean state
  - Scenario: --dry-run flag accepted without effect
"""

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _write_meeting_note(
    path: Path,
    *,
    with_frontmatter: bool = True,
    with_source_id: bool = True,
) -> None:
    """Write a synthetic meeting note to *path*."""
    if not with_frontmatter:
        path.write_text("# Meeting notes\n\nSome content here.\n", encoding="utf-8")
        return

    lines = ["---"]
    if with_source_id:
        lines.append("source_id: DOC_TEST_0001")
    lines += ["title: Test Meeting", "---", "", "# Notes", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _make_accounts_dir(tmp_path: Path, accounts: list[str]) -> Path:
    """Create accounts/ directory tree under tmp_path and return accounts_root."""
    accounts_root = tmp_path / "accounts"
    for account in accounts:
        (accounts_root / account / "meetings").mkdir(parents=True)
    return accounts_root


# ── TestBackfillHintPerCandidate (flattened) ────────────────────────────────


def test_backfill_hint_per_candidate_hint_line_present_for_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A candidate missing source_id must be followed by a → hint line."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "→ fieldkit ingest run" in captured.out


def test_backfill_hint_per_candidate_hint_line_contains_pipeline_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hint must reference the transcript-ingest pipeline ID."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main([])

    captured = capsys.readouterr()
    assert "transcript-ingest" in captured.out


def test_backfill_hint_per_candidate_hint_line_uses_pipeline_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hint must use the --pipeline flag (verified against run.py)."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main([])

    captured = capsys.readouterr()
    assert "--pipeline" in captured.out


def test_backfill_hint_per_candidate_hint_appears_after_candidate_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hint line must appear immediately after the candidate path/reason line."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main([])

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    # Find the candidate line (contains the filename)
    candidate_idx = next((i for i, ln in enumerate(lines) if "2026-05-01-planning.md" in ln), None)
    assert candidate_idx is not None, "Candidate line not found in output"
    # The very next non-empty line must be the hint
    assert candidate_idx + 1 < len(lines), "No line after candidate line"
    assert "→ fieldkit ingest run" in lines[candidate_idx + 1]


def test_backfill_hint_per_candidate_multiple_candidates_each_get_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every candidate must have its own hint line."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme", "globalpay"])
    for account in ["acme", "globalpay"]:
        note = accounts_root / account / "meetings" / "2026-05-10-q2.md"
        _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    hint_count = captured.out.count("→ fieldkit ingest run")
    assert hint_count == 2, f"Expected 2 hint lines, got {hint_count}"


def test_backfill_hint_per_candidate_summary_mentions_provenance_when_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Summary line must reference provenance assignment when candidates exist."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main([])

    captured = capsys.readouterr()
    assert "provenance" in captured.out.lower()


# ── TestBackfillNoCandidatesNoHint (flattened) ──────────────────────────────


def test_backfill_no_candidates_no_hint_no_hint_when_no_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With all notes having source_id, no hint line must appear."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "→" not in captured.out


def test_backfill_no_candidates_no_hint_summary_reads_zero_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Summary must read '0 candidates found' with no next-step hint."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main([])

    captured = capsys.readouterr()
    assert "0 candidates found" in captured.out
    # No provenance hint in summary for the zero-candidate case
    assert "provenance" not in captured.out.lower()


def test_backfill_no_candidates_no_hint_empty_accounts_dir_no_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty accounts/ directory → 0 candidates, no hint."""
    _make_accounts_dir(tmp_path, ["acme"])  # no meeting files

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "→" not in captured.out
    assert "0 candidates found" in captured.out


# ── TestBackfillDryRunConsistency (flattened) ───────────────────────────────


def test_backfill_dry_run_consistency_dry_run_same_hint_as_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run must show the same → hint as the live invocation."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "→ fieldkit ingest run" in captured.out


def test_backfill_dry_run_consistency_dry_run_no_candidates_no_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run with zero candidates must also produce no hint."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "→" not in captured.out


# ---------------------------------------------------------------------------
# historic regression: backfill NOTE explains pipeline.db vs filesystem gap
# ---------------------------------------------------------------------------


# ── TestBug349BackfillNote (flattened) ──────────────────────────────────────


def test_bug349_backfill_note_note_shown_when_candidates_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NOTE about pipeline.db is shown when candidates are found."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "pipeline.db" in captured.out, "Expected NOTE about pipeline.db in output, got:\n" + captured.out
    assert "ingest route" in captured.out


def test_bug349_backfill_note_note_not_shown_when_no_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NOTE is NOT shown when there are no candidates (clean state)."""
    accounts_root = _make_accounts_dir(tmp_path, ["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "pipeline.db" not in captured.out


# ---------------------------------------------------------------------------
# 4F.3 scan_vault
# ---------------------------------------------------------------------------


# ── TestScanVault (flattened) ───────────────────────────────────────────────


@pytest.mark.unit
def test_scan_vault_scan_vault_returns_list(tmp_path: Path) -> None:
    """scan_vault returns a list."""
    from fieldkit.commands.ingest.backfill import scan_vault

    result = scan_vault(tmp_path)
    assert isinstance(result, list)


@pytest.mark.unit
def test_scan_vault_scan_vault_empty_dir_returns_empty(tmp_path: Path) -> None:
    """scan_vault returns [] when no meeting notes found."""
    from fieldkit.commands.ingest.backfill import scan_vault

    result = scan_vault(tmp_path)
    assert result == []


@pytest.mark.unit
def test_scan_vault_scan_vault_finds_md_files_without_source(tmp_path: Path) -> None:
    """scan_vault returns CandidateFile entries for files missing source stamps."""
    from fieldkit.commands.ingest.backfill import scan_vault

    # Create the meetings structure expected by scan_vault: accounts_root/account/meetings/*.md
    meetings_dir = tmp_path / "acme" / "meetings"
    meetings_dir.mkdir(parents=True)
    # Write files without source stamp (no frontmatter)
    (meetings_dir / "2026-01-01-call.md").write_text("# Meeting\nNotes here.", encoding="utf-8")
    (meetings_dir / "2026-01-02-review.md").write_text("# Review\nMore notes.", encoding="utf-8")

    result = scan_vault(tmp_path)
    assert len(result) == 2
    assert all(c.account == "acme" for c in result)
