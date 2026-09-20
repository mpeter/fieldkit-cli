"""Tests for fieldkit pursuit rename command."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from click.testing import Result as CliResult

from fieldkit.commands.pursuit.rename_cmd import cli


def _make_pursuit(pursuits_dir: Path, slug: str, stage: str = "discover") -> Path:
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nstage: {stage}\ngate-status: pending\n---\n\n# {slug}\n"
    path = pursuits_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _make_stall_state(watchers_dir: Path, account: str, slug: str) -> Path:
    watchers_dir.mkdir(parents=True, exist_ok=True)
    state = {
        f"{account}/{slug}": {
            "account": account,
            "pursuit": slug,
            "stage": "discover",
            "is_stalled": True,
            "alerted_days_tier": 14,
        }
    }
    path = watchers_dir / "pursuit-stall-state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def _make_countdown_state(watchers_dir: Path, account: str, slug: str) -> Path:
    watchers_dir.mkdir(parents=True, exist_ok=True)
    state = {
        f"{account}/{slug}": {
            "account": account,
            "pursuit": slug,
            "alerted_tier": "red",
        }
    }
    path = watchers_dir / "close-date-countdown-state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    account: str = "acme",
    from_slug: str = "old-deal",
    to_slug: str = "new-deal",
    extra_args: list[str] | None = None,
) -> CliResult:
    runner = CliRunner()
    args = ["--account", account, "--from", from_slug, "--to", to_slug]
    if extra_args:
        args.extend(extra_args)
    with patch("fieldkit.commands.pursuit.rename_cmd._data_root", return_value=tmp_path):
        return runner.invoke(cli, args, catch_exceptions=False)


# ── TestRenameHappyPath (flattened) ─────────────────────────────────────────


@pytest.mark.unit
def test_rename_happy_path_renames_file_and_stall_state(tmp_path: Path) -> None:
    """Happy path: file renamed and stall state key updated."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    watchers = tmp_path / "watchers"
    _make_pursuit(pursuits, "old-deal")
    _make_stall_state(watchers, "acme", "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    assert not (pursuits / "old-deal.md").exists()
    assert (pursuits / "new-deal.md").exists()

    state = json.loads((watchers / "pursuit-stall-state.json").read_text(encoding="utf-8"))
    assert "acme/old-deal" not in state
    assert "acme/new-deal" in state
    assert state["acme/new-deal"]["pursuit"] == "new-deal"


@pytest.mark.unit
def test_rename_happy_path_renames_countdown_state_too(tmp_path: Path) -> None:
    """Countdown state key is also renamed when present."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    watchers = tmp_path / "watchers"
    _make_pursuit(pursuits, "old-deal")
    _make_stall_state(watchers, "acme", "old-deal")
    _make_countdown_state(watchers, "acme", "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    countdown = json.loads((watchers / "close-date-countdown-state.json").read_text(encoding="utf-8"))
    assert "acme/old-deal" not in countdown
    assert "acme/new-deal" in countdown


@pytest.mark.unit
def test_rename_happy_path_missing_stall_state_silently_skipped(tmp_path: Path) -> None:
    """No stall state file → rename still succeeds."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    assert (pursuits / "new-deal.md").exists()


@pytest.mark.unit
def test_rename_happy_path_missing_countdown_state_silently_skipped(tmp_path: Path) -> None:
    """Stall state present but countdown missing → still succeeds."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    watchers = tmp_path / "watchers"
    _make_pursuit(pursuits, "old-deal")
    _make_stall_state(watchers, "acme", "old-deal")
    # no countdown state file

    result = _run(tmp_path)

    assert result.exit_code == 0
    assert not (pursuits / "old-deal.md").exists()


# ── TestRenameDryRun (flattened) ────────────────────────────────────────────


@pytest.mark.unit
def test_rename_dry_run_dry_run_does_not_modify_anything(tmp_path: Path) -> None:
    """--dry-run previews without making changes."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    watchers = tmp_path / "watchers"
    _make_pursuit(pursuits, "old-deal")
    _make_stall_state(watchers, "acme", "old-deal")

    result = _run(tmp_path, extra_args=["--dry-run"])

    assert result.exit_code == 0
    assert (pursuits / "old-deal.md").exists()  # not moved
    assert not (pursuits / "new-deal.md").exists()
    assert "dry-run" in result.output

    # State JSON unchanged
    state = json.loads((watchers / "pursuit-stall-state.json").read_text(encoding="utf-8"))
    assert "acme/old-deal" in state
    assert "acme/new-deal" not in state


# ── TestRenameErrors (flattened) ────────────────────────────────────────────


@pytest.mark.unit
def test_rename_errors_source_not_found_exits_3(tmp_path: Path) -> None:
    """Missing source file → exit 3."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)

    result = _run(tmp_path)

    assert result.exit_code == 3
    assert "not found" in result.output.lower()


@pytest.mark.unit
def test_rename_errors_target_already_exists_exits_1(tmp_path: Path) -> None:
    """Target slug already exists → exit 1."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")
    _make_pursuit(pursuits, "new-deal")  # already exists

    result = _run(tmp_path)

    assert result.exit_code == 1
    assert "already exists" in result.output.lower()


@pytest.mark.unit
def test_rename_errors_account_dir_not_found_exits_3(tmp_path: Path) -> None:
    """Non-existent account → exit 3."""
    result = _run(tmp_path, account="nonexistent-account")

    assert result.exit_code == 3
    assert "not found" in result.output.lower()


# ── TestRenameNextSteps (flattened) ─────────────────────────────────────────


@pytest.mark.unit
def test_rename_next_steps_next_steps_printed(tmp_path: Path) -> None:
    """Output includes next-steps guidance."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    assert "Next steps" in result.output
    assert "fieldkit sf opportunity" in result.output
    assert "new-deal" in result.output


# ---------------------------------------------------------------------------
# implementation change: rename next-steps output shows exact grep command
# ---------------------------------------------------------------------------


# ── TestRenameGrepHint (flattened) ──────────────────────────────────────────


@pytest.mark.unit
def test_rename_grep_hint_grep_command_in_normal_output(tmp_path: Path) -> None:
    """Successful rename output contains grep -r <shlex.quote(old-slug)>."""
    import shlex

    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    # shlex.quote("old-deal") → "old-deal" (no quotes needed for safe strings)
    assert f"grep -r {shlex.quote('old-deal')}" in result.output


@pytest.mark.unit
def test_rename_grep_hint_grep_command_references_account_dir(tmp_path: Path) -> None:
    """grep command must include the account directory path so it's copy-paste ready."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    # The accounts/acme/ path must appear in the grep command
    assert "acme" in result.output


@pytest.mark.unit
def test_rename_grep_hint_grep_command_in_dry_run_output(tmp_path: Path) -> None:
    """--dry-run output also contains the grep command referencing the old slug."""
    import shlex

    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path, extra_args=["--dry-run"])

    assert result.exit_code == 0
    assert f"grep -r {shlex.quote('old-deal')}" in result.output


@pytest.mark.unit
def test_rename_grep_hint_grep_command_uses_from_slug_not_to_slug(tmp_path: Path) -> None:
    """grep command must reference the OLD slug (what we're searching for), not the new one."""
    import shlex

    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path, from_slug="old-deal", to_slug="new-deal")

    assert result.exit_code == 0
    assert f"grep -r {shlex.quote('old-deal')}" in result.output


# ---------------------------------------------------------------------------
# implementation change review fix: grep hint uses relative path and shlex.quote()
# ---------------------------------------------------------------------------


# ── TestRenameGrepHintRelativeAndQuoted (flattened) ─────────────────────────


@pytest.mark.unit
def test_rename_grep_hint_relative_and_quoted_grep_hint_uses_relative_accounts_path(tmp_path: Path) -> None:
    """grep hint must contain 'accounts/' (relative), not an absolute filesystem path.

    The absolute data-root path (e.g. <user-home-path>/fieldkit-data/) must NOT
    appear in the grep command — the AE runs it from the data-repo root.
    """
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "old-deal")

    result = _run(tmp_path)

    assert result.exit_code == 0
    # Relative path must be present in the output
    assert "accounts/" in result.output
    # Absolute tmp_path must NOT appear inside the grep command line
    # (it may appear elsewhere in error messages, so we check the grep line)
    grep_line = next(
        (ln for ln in result.output.splitlines() if "grep -r" in ln),
        None,
    )
    assert grep_line is not None, "grep -r line not found in output"
    assert str(tmp_path) not in grep_line, f"Absolute path leaked into grep hint: {grep_line!r}"


@pytest.mark.unit
def test_rename_grep_hint_relative_and_quoted_grep_hint_quotes_slug_with_single_quote(tmp_path: Path) -> None:
    """A slug containing a single quote must be shell-quoted in the grep hint.

    shlex.quote() uses POSIX quoting: a single quote inside a single-quoted
    string is escaped as '"'"' (end quote, double-quoted apostrophe, reopen
    quote), producing 'it'"'"'s-a-deal'.  The raw unescaped form "it's-a-deal"
    must NOT appear verbatim — that would be a shell syntax error.
    """
    import shlex

    tricky_slug = "it's-a-deal"
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, tricky_slug)

    result = _run(tmp_path, from_slug=tricky_slug, to_slug="new-deal")

    assert result.exit_code == 0
    grep_line = next(
        (ln for ln in result.output.splitlines() if "grep -r" in ln),
        None,
    )
    assert grep_line is not None, "grep -r line not found in output"
    # The raw unquoted slug must not appear verbatim in the hint
    assert tricky_slug not in grep_line, f"Raw unquoted slug appeared in grep hint: {grep_line!r}"
    # The properly shell-quoted form must appear instead
    expected_quoted = shlex.quote(tricky_slug)
    assert expected_quoted in grep_line, (
        f"Expected shlex.quote({tricky_slug!r}) = {expected_quoted!r} in grep hint: {grep_line!r}"
    )
