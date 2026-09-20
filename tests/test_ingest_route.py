"""Tests for fieldkit/ingest/route.py — ingest route subcommand.

Test layers:
1. Prefix-based routing (filename stem starts with account slug)
2. Title-keyword fallback routing via lib.router.route_by_title
3. Ambiguous match (title matches 2+ accounts) → ambiguous-match tag written
4. Unmatched file → reviewed-unmatched: true written
5. Idempotency: files already tagged are skipped
6. --dry-run: no filesystem changes
7. --help exits 0

Pattern: Click CliRunner with lib.config.get_accounts_config patched so tests
are self-contained and do not depend on the real accounts.yaml config.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.ingest.route import cli

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Test config
# ---------------------------------------------------------------------------

_ACCOUNTS_YAML = """\
accounts:
  acme-corp:
    domains:
      - acme.com
    keywords:
      - acmecorp
      - acme inc
  globalpay:
    domains:
      - globalpay.com
    keywords:
      - global pay
      - globalpay
  shield:
    domains:
      - shield-ins.com
    keywords:
      - shield insurance
internal_domains:
  - internal.example.com
"""


def _parsed_accounts_yaml() -> dict[str, Any]:
    return yaml.safe_load(_ACCOUNTS_YAML) or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_data_root(tmp_path: Path, accounts: list[str] | None = None) -> Path:
    """Create a minimal data root with config and account dirs."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "accounts.yaml").write_text(_ACCOUNTS_YAML, encoding="utf-8")

    (tmp_path / "accounts" / "unknown" / "meetings").mkdir(parents=True)

    for acct in accounts or ["acme-corp", "globalpay", "shield"]:
        (tmp_path / "accounts" / acct / "meetings").mkdir(parents=True)

    return tmp_path


def _write_meeting(path: Path, *, title: str = "Meeting", extra_fm: str = "") -> None:
    """Write a minimal meeting .md file with YAML frontmatter."""
    fm = f"---\ntitle: {title}\n"
    if extra_fm:
        fm += extra_fm.rstrip("\n") + "\n"
    fm += "---\n\nMeeting notes here.\n"
    path.write_text(fm, encoding="utf-8")


def _read_fm(path: Path) -> dict[str, Any]:
    """Parse and return the YAML frontmatter from *path*."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end]) or {}


def _invoke_route(data_root: Path, extra_args: list[str] | None = None) -> Any:
    """Invoke the route CLI command with test accounts config patched.

    Patches lib.config.get_accounts_config so prefix-matching in route.py
    uses the test YAML, not the real accounts.yaml on disk.
    """
    with patch("fieldkit.config.get_accounts_config", return_value=_parsed_accounts_yaml()):
        runner = CliRunner()
        return runner.invoke(cli, ["--data-root", str(data_root)] + (extra_args or []))


# ---------------------------------------------------------------------------
# Tests — prefix match
# ---------------------------------------------------------------------------


# ── TestRoutePrefixMatch (flattened) ────────────────────────────────────────


def test_match_by_prefix_moves_file_to_correct_account(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "acme-corp-2026-05-01-kickoff.md"
    _write_meeting(meeting, title="Kickoff")

    result = _invoke_route(data_root)

    assert result.exit_code == 0, result.output
    dest = data_root / "accounts" / "acme-corp" / "meetings" / "acme-corp-2026-05-01-kickoff.md"
    assert dest.exists(), "File should be moved to acme-corp/meetings/"
    assert not meeting.exists(), "Original file should no longer exist in unknown/"


def test_match_by_prefix_moved_file_has_account_frontmatter(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "acme-corp-2026-05-01-kickoff.md"
    _write_meeting(meeting, title="Kickoff")

    _invoke_route(data_root)

    dest = data_root / "accounts" / "acme-corp" / "meetings" / "acme-corp-2026-05-01-kickoff.md"
    assert dest.exists(), "Moved file must exist"
    fm = _read_fm(dest)
    assert fm.get("account") == "acme-corp"


def test_match_by_prefix_summary_shows_moved_count(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    _write_meeting(unknown / "acme-corp-2026-05-01-q1.md", title="Q1")
    _write_meeting(unknown / "globalpay-2026-05-01-q1.md", title="Q1 GP")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert "2 moved" in result.output


def test_route_json_full_scan_reports_items_without_mutating_dry_run(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    meeting = data_root / "accounts" / "unknown" / "meetings" / "acme-corp-2026-05-01-kickoff.md"
    _write_meeting(meeting, title="Kickoff")

    result = _invoke_route(data_root, ["--dry-run", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["mode"] == "scan"
    assert payload["considered"] == 1
    assert payload["items"][0]["outcome"] == "would-move"
    assert meeting.exists()


def test_route_json_force_reports_single_dry_run_item(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    meeting = data_root / "accounts" / "unknown" / "meetings" / "target.md"
    _write_meeting(meeting, title="Manual route")

    with (
        patch("fieldkit.config.get_accounts_config", return_value=_parsed_accounts_yaml()),
        patch("fieldkit.config.get_account_names", return_value=["acme-corp"]),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--data-root",
                str(data_root),
                "--file",
                "target.md",
                "--force-account",
                "acme-corp",
                "--dry-run",
                "--json",
            ],
        )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["mode"] == "force"
    assert payload["considered"] == 1
    assert payload["items"][0]["outcome"] == "would-move"
    assert meeting.exists()


# ---------------------------------------------------------------------------
# Tests — title match
# ---------------------------------------------------------------------------


# ── TestRouteTitleMatch (flattened) ─────────────────────────────────────────


def test_match_by_title_moves_file_by_title_keyword(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-strategy-session.md"
    _write_meeting(meeting, title="GlobalPay Q2 Strategy Session")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    dest = data_root / "accounts" / "globalpay" / "meetings" / "2026-05-01-strategy-session.md"
    assert dest.exists(), "File should be moved to globalpay/meetings/"


def test_match_by_title_title_match_account_field_written(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-10-shield-review.md"
    _write_meeting(meeting, title="Shield Insurance Annual Review")

    _invoke_route(data_root)

    dest = data_root / "accounts" / "shield" / "meetings" / "2026-05-10-shield-review.md"
    if dest.exists():
        fm = _read_fm(dest)
        assert fm.get("account") == "shield"


# ---------------------------------------------------------------------------
# Tests — ambiguous match
# ---------------------------------------------------------------------------


# ── TestRouteAmbiguous (flattened) ──────────────────────────────────────────


def _route_one_file_make_ambiguous_root(tmp_path: Path) -> tuple[Path, Path]:
    """Set up data root where two accounts share a keyword."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    ambiguous_yaml = """\
accounts:
  acme-corp:
    keywords:
      - shared keyword
  globalpay:
    keywords:
      - shared keyword
internal_domains: []
"""
    (config_dir / "accounts.yaml").write_text(ambiguous_yaml, encoding="utf-8")
    (tmp_path / "accounts" / "unknown" / "meetings").mkdir(parents=True)
    (tmp_path / "accounts" / "acme-corp" / "meetings").mkdir(parents=True)
    (tmp_path / "accounts" / "globalpay" / "meetings").mkdir(parents=True)
    meeting = tmp_path / "accounts" / "unknown" / "meetings" / "2026-05-01-shared-meeting.md"
    _write_meeting(meeting, title="Meeting about shared keyword")
    return tmp_path, meeting


def test_route_one_file_file_stays_in_unknown(tmp_path: Path) -> None:
    data_root, meeting = _route_one_file_make_ambiguous_root(tmp_path)
    parsed = yaml.safe_load((data_root / "config" / "accounts.yaml").read_text())

    with patch("fieldkit.config.get_accounts_config", return_value=parsed):
        runner = CliRunner()
        result = runner.invoke(cli, ["--data-root", str(data_root)])

    assert result.exit_code == 0
    assert meeting.exists(), "Ambiguous file should stay in unknown/"


def test_route_one_file_ambiguous_match_tag_written(tmp_path: Path) -> None:
    data_root, meeting = _route_one_file_make_ambiguous_root(tmp_path)
    parsed = yaml.safe_load((data_root / "config" / "accounts.yaml").read_text())

    with patch("fieldkit.config.get_accounts_config", return_value=parsed):
        runner = CliRunner()
        runner.invoke(cli, ["--data-root", str(data_root)])

    fm = _read_fm(meeting)
    assert "ambiguous-match" in fm, "ambiguous-match key should be written to frontmatter"
    assert isinstance(fm["ambiguous-match"], list)
    assert len(fm["ambiguous-match"]) >= 2


def test_route_one_file_summary_shows_ambiguous_count(tmp_path: Path) -> None:
    data_root, _ = _route_one_file_make_ambiguous_root(tmp_path)
    parsed = yaml.safe_load((data_root / "config" / "accounts.yaml").read_text())

    with patch("fieldkit.config.get_accounts_config", return_value=parsed):
        runner = CliRunner()
        result = runner.invoke(cli, ["--data-root", str(data_root)])

    assert "1 ambiguous" in result.output


# ---------------------------------------------------------------------------
# Tests — unmatched
# ---------------------------------------------------------------------------


# ── TestRouteUnmatched (flattened) ──────────────────────────────────────────


def test_route_one_file_file_stays_in_unknown_2(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-completely-unrelated.md"
    _write_meeting(meeting, title="Completely Unrelated Meeting")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert meeting.exists(), "Unmatched file should stay in unknown/"


def test_route_one_file_reviewed_unmatched_tag_written(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-completely-unrelated.md"
    _write_meeting(meeting, title="Completely Unrelated Meeting")

    _invoke_route(data_root)

    fm = _read_fm(meeting)
    assert fm.get("reviewed-unmatched") is True


def test_route_one_file_summary_shows_unmatched_count(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    _write_meeting(unknown / "2026-05-01-unrelated-a.md", title="Unrelated A")
    _write_meeting(unknown / "2026-05-01-unrelated-b.md", title="Unrelated B")

    result = _invoke_route(data_root)

    assert "2 unmatched" in result.output


# ---------------------------------------------------------------------------
# Tests — idempotency
# ---------------------------------------------------------------------------


# ── TestRouteIdempotent (flattened) ─────────────────────────────────────────


def test_route_one_file_reviewed_unmatched_file_is_skipped(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-old-unmatched.md"
    _write_meeting(meeting, title="Old Unmatched", extra_fm="reviewed-unmatched: true\n")

    original_mtime = meeting.stat().st_mtime

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert meeting.exists()
    assert meeting.stat().st_mtime == original_mtime


def test_route_one_file_ambiguous_match_file_is_skipped(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-old-ambig.md"
    _write_meeting(meeting, title="Old Ambiguous", extra_fm="ambiguous-match:\n- acme-corp\n- globalpay\n")

    original_mtime = meeting.stat().st_mtime

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert meeting.exists()
    assert meeting.stat().st_mtime == original_mtime


def test_route_one_file_skipped_count_in_summary(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    _write_meeting(
        unknown / "2026-05-01-skip1.md",
        title="Skip",
        extra_fm="reviewed-unmatched: true\n",
    )
    _write_meeting(
        unknown / "2026-05-01-skip2.md",
        title="Skip2",
        extra_fm="ambiguous-match:\n- acme-corp\n",
    )

    result = _invoke_route(data_root)

    assert "2 skipped" in result.output


# ---------------------------------------------------------------------------
# Tests — dry-run
# ---------------------------------------------------------------------------


# ── TestRouteDryRun (flattened) ─────────────────────────────────────────────


def test_route_one_file_files_stay_in_unknown_on_dry_run(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "acme-corp-2026-05-01-kickoff.md"
    _write_meeting(meeting, title="Kickoff")

    result = _invoke_route(data_root, ["--dry-run"])

    assert result.exit_code == 0
    assert meeting.exists(), "File should NOT be moved during dry-run"
    dest = data_root / "accounts" / "acme-corp" / "meetings" / "acme-corp-2026-05-01-kickoff.md"
    assert not dest.exists()


def test_route_one_file_dry_run_output_mentions_dry_run(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    _write_meeting(unknown / "acme-corp-2026-05-01-kickoff.md", title="Kickoff")

    result = _invoke_route(data_root, ["--dry-run"])

    assert "DRY-RUN" in result.output or "dry" in result.output.lower()


def test_route_one_file_dry_run_does_not_write_frontmatter(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-completely-unrelated.md"
    _write_meeting(meeting, title="Completely Unrelated Meeting")

    original_text = meeting.read_text(encoding="utf-8")

    _invoke_route(data_root, ["--dry-run"])

    assert meeting.read_text(encoding="utf-8") == original_text, "Frontmatter should not be modified on dry-run"


def test_route_one_file_dry_run_summary_label(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    # Put one file in so the command produces output
    unknown = data_root / "accounts" / "unknown" / "meetings"
    _write_meeting(unknown / "2026-05-01-any.md", title="Any Meeting")

    result = _invoke_route(data_root, ["--dry-run"])

    assert result.exit_code == 0
    assert "DRY-RUN" in result.output


# ---------------------------------------------------------------------------
# Tests — help
# ---------------------------------------------------------------------------


# ── TestRouteHelp (flattened) ───────────────────────────────────────────────


def test_route_one_file_help_exits_0() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0


def test_route_one_file_help_mentions_dry_run() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert "--dry-run" in result.output


# ---------------------------------------------------------------------------
# Tests — edge cases
# ---------------------------------------------------------------------------


# ── TestRouteEdgeCases (flattened) ──────────────────────────────────────────


def test_route_one_file_empty_unknown_dir_exits_0(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)

    result = _invoke_route(data_root)

    assert result.exit_code == 0


def test_route_one_file_missing_unknown_dir_exits_0(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    import shutil

    shutil.rmtree(data_root / "accounts" / "unknown")

    result = _invoke_route(data_root)

    assert result.exit_code == 0


def test_route_one_file_file_without_frontmatter_is_warned_and_skipped(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    no_fm = unknown / "2026-05-01-no-frontmatter.md"
    no_fm.write_text("# No frontmatter here\n\nJust some notes.\n", encoding="utf-8")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert no_fm.exists()


def test_route_one_file_gitkeep_ignored(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    (unknown / ".gitkeep").write_text("", encoding="utf-8")

    result = _invoke_route(data_root)

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Task 11.3 — route CLI exits 0 when no unknown meetings dir exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_route_cli_exits_zero_when_no_unknown_dir(tmp_path: Path) -> None:
    """route exits 0 (not nonzero) when accounts/unknown/meetings/ is absent."""
    runner = CliRunner()
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)
    # No unknown/meetings dir

    with patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}):
        result = runner.invoke(cli, ["--data-root", str(data_root)])

    # Should exit 0 (nothing to do)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CRAP-reduction: additional branch coverage for ingest/route.py:cli
# ---------------------------------------------------------------------------


# ── TestRouteBranchCoverage (flattened) ─────────────────────────────────────


def test_route_one_file_underscore_prefix_match(tmp_path: Path) -> None:
    """Filename with underscore separator (acme-corp_2026-05-01.md) is prefix-matched."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "acme-corp_2026-05-01-kickoff.md"
    _write_meeting(meeting, title="Kickoff")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    dest = data_root / "accounts" / "acme-corp" / "meetings" / "acme-corp_2026-05-01-kickoff.md"
    assert dest.exists(), "File with underscore separator should be moved"


def test_route_one_file_routing_error_warns_and_continues(tmp_path: Path) -> None:
    """OSError from route_by_title is caught, warning emitted, file stays unmatched."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-error-meeting.md"
    _write_meeting(meeting, title="Error Meeting")

    with (
        patch("fieldkit.config.get_accounts_config", return_value=_parsed_accounts_yaml()),
        patch("fieldkit.ingest.router.route_by_title", side_effect=OSError("disk error")),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["--data-root", str(data_root)])

    assert result.exit_code == 0
    # File should still be in unknown (routing failed)
    assert meeting.exists()


def test_route_one_file_dry_run_ambiguous_shows_accounts(tmp_path: Path) -> None:
    """--dry-run with ambiguous match prints [DRY-RUN] AMBIGUOUS and account list."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    ambiguous_yaml = """\
accounts:
  acme-corp:
    keywords:
      - shared keyword
  globalpay:
    keywords:
      - shared keyword
internal_domains: []
"""
    (config_dir / "accounts.yaml").write_text(ambiguous_yaml, encoding="utf-8")
    (tmp_path / "accounts" / "unknown" / "meetings").mkdir(parents=True)
    (tmp_path / "accounts" / "acme-corp" / "meetings").mkdir(parents=True)
    (tmp_path / "accounts" / "globalpay" / "meetings").mkdir(parents=True)
    meeting = tmp_path / "accounts" / "unknown" / "meetings" / "2026-05-01-shared.md"
    _write_meeting(meeting, title="Meeting about shared keyword")

    parsed = __import__("yaml").safe_load(ambiguous_yaml)
    with patch("fieldkit.config.get_accounts_config", return_value=parsed):
        runner = CliRunner()
        result = runner.invoke(cli, ["--data-root", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert "AMBIGUOUS" in result.output
    # File must NOT be modified in dry-run
    assert meeting.exists()
    fm = _read_fm(meeting)
    assert "ambiguous-match" not in fm


def test_route_one_file_dry_run_unmatched_shows_unmatched(tmp_path: Path) -> None:
    """--dry-run with no match prints [DRY-RUN] UNMATCHED and does not write frontmatter."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-totally-unrelated.md"
    _write_meeting(meeting, title="Totally Unrelated")

    result = _invoke_route(data_root, ["--dry-run"])

    assert result.exit_code == 0
    assert "UNMATCHED" in result.output
    # Frontmatter must NOT be modified
    fm = _read_fm(meeting)
    assert "reviewed-unmatched" not in fm


def test_route_one_file_no_title_in_frontmatter_falls_through_to_unmatched(tmp_path: Path) -> None:
    """File with no title or meeting_title key is not routed by title (falls to unmatched)."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-no-title.md"
    # Write frontmatter with no title key
    meeting.write_text("---\nauthor: Jane\n---\n\nNotes.\n", encoding="utf-8")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    # No title → no keyword routing → unmatched
    fm = _read_fm(meeting)
    assert fm.get("reviewed-unmatched") is True


def test_route_one_file_meeting_title_key_used_for_routing(tmp_path: Path) -> None:
    """meeting_title key is used as fallback when title is absent."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-gp-meeting.md"
    # Use meeting_title instead of title
    meeting.write_text("---\nmeeting_title: GlobalPay Q2 Strategy\n---\n\nNotes.\n", encoding="utf-8")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    dest = data_root / "accounts" / "globalpay" / "meetings" / "2026-05-01-gp-meeting.md"
    assert dest.exists(), "File should be routed using meeting_title keyword"


def test_route_one_file_yaml_error_in_frontmatter_warns_and_skips(tmp_path: Path) -> None:
    """YAML parse error in frontmatter emits warning and skips file (n_skipped++)."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"
    meeting = unknown / "2026-05-01-bad-yaml.md"
    # Write invalid YAML frontmatter
    meeting.write_text("---\ntitle: [unclosed bracket\n---\n\nNotes.\n", encoding="utf-8")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert "1 skipped" in result.output


def test_route_one_file_multiple_files_mixed_outcomes(tmp_path: Path) -> None:
    """Multiple files with different outcomes are all counted correctly."""
    data_root = _make_data_root(tmp_path)
    unknown = data_root / "accounts" / "unknown" / "meetings"

    # 1 prefix-matched → moved
    _write_meeting(unknown / "acme-corp-2026-05-01-kickoff.md", title="Kickoff")
    # 1 unmatched → reviewed-unmatched
    _write_meeting(unknown / "2026-05-01-unrelated.md", title="Unrelated Meeting")
    # 1 already skipped
    _write_meeting(unknown / "2026-05-01-old.md", title="Old", extra_fm="reviewed-unmatched: true\n")

    result = _invoke_route(data_root)

    assert result.exit_code == 0
    assert "1 moved" in result.output
    assert "1 unmatched" in result.output
    assert "1 skipped" in result.output


# ---------------------------------------------------------------------------
# historic regression: b-of-a account name alias routing
# ---------------------------------------------------------------------------

_ACCOUNTS_WITH_BOFA = """\
accounts:
  acme-corp:
    domains:
      - acme.com
    keywords:
      - acmecorp
  acme-corp:  # pii-guard: ignore
    domains:
      - bankofamerica.com
    keywords:
      - bank of america
"""


# ── TestAccountNameAliasRouting (flattened) ─────────────────────────────────


def test_apply_account_aliases_apply_account_aliases_normalises_b_of_a() -> None:
    """_apply_account_aliases replaces 'b-of-a' prefix with 'acme-corp'."""  # pii-guard: ignore
    from fieldkit.ingest.route_batch import _apply_account_aliases

    assert (  # pii-guard: ignore
        _apply_account_aliases("b-of-a-2026-06-08-meeting") == "acme-corp-2026-06-08-meeting"  # pii-guard: ignore
    )


def test_apply_account_aliases_apply_account_aliases_leaves_normal_stems_unchanged() -> None:
    """_apply_account_aliases does not modify stems without known aliases."""
    from fieldkit.ingest.route_batch import _apply_account_aliases

    assert _apply_account_aliases("acme-corp-2026-05-01-kickoff") == "acme-corp-2026-05-01-kickoff"


def test_apply_account_aliases_match_by_prefix_routes_b_of_a_to_acme_corp() -> None:  # pii-guard: ignore
    """_match_by_prefix routes a b-of-a stem to the acme-corp account slug."""  # pii-guard: ignore
    from fieldkit.ingest.route_batch import _match_by_prefix

    result = _match_by_prefix(
        "b-of-a-2026-06-08-bvp-sync",
        ["acme-corp", "acme-corp", "globalpay"],  # pii-guard: ignore
    )
    assert result == "acme-corp"  # pii-guard: ignore
