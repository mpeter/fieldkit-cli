"""Tests for --dry-run non-interactive fail-fast behavior (implementation change)."""

import pytest

from fieldkit.commands.skill._runner import _cmd_install


@pytest.mark.unit
def test_dry_run_without_tool_or_skill_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    result = _cmd_install([], [], install_all=False, dry_run=True)
    assert result == 1
    captured = capsys.readouterr()
    assert "--dry-run requires --tool and --skill" in captured.err


@pytest.mark.unit
def test_dry_run_without_tool_only_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    result = _cmd_install([], ["brief"], install_all=False, dry_run=True)
    assert result == 1
    captured = capsys.readouterr()
    assert "--dry-run requires --tool and --skill" in captured.err


@pytest.mark.unit
def test_dry_run_without_skill_only_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    result = _cmd_install(["claude-code"], [], install_all=False, dry_run=True)
    assert result == 1
    captured = capsys.readouterr()
    assert "--dry-run requires --tool and --skill" in captured.err


@pytest.mark.unit
def test_dry_run_with_all_but_no_tool_still_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    # --all only substitutes for --skill, never --tool (see _cmd_install docstring).
    # A future edit that exempts install_all from the guard (e.g.
    # `if dry_run and not install_all and (not tools or not skills):`) would
    # silently reopen the CI-hang bug this guard exists to prevent — this locks
    # in the current, correct behavior.
    result = _cmd_install([], [], install_all=True, dry_run=True)
    assert result == 1
    captured = capsys.readouterr()
    assert "--dry-run requires --tool and --skill" in captured.err


@pytest.mark.unit
def test_non_dry_run_does_not_trigger_the_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Non-dry-run calls with no --tool/--skill must NOT hit the new guard —
    # they fall through to interactive selection as before (unaffected by this
    # fix). Feed an empty response to the numbered-select prompt so the
    # (pre-existing, out-of-scope) interactive path resolves deterministically.
    monkeypatch.setattr("builtins.input", lambda *_args: "")
    result = _cmd_install([], [], install_all=False, dry_run=False)
    captured = capsys.readouterr()
    assert "--dry-run requires --tool and --skill" not in captured.err
    assert result == 0
