"""Unit tests for historic regression: validate_pursuit.py calls fieldkit sf frontmatter.

Verifies that the hook invokes 'fieldkit sf frontmatter' (not the dead
'sf_pipeline' module) and exits 0 on success (advisory-only hook).
"""

import sys
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _clean_proc() -> CompletedProcess:  # type: ignore[type-arg]
    return CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ── TestValidatePursuitHookCommand (flattened) ──────────────────────────────


def test_validate_pursuit_hook_command_validate_call_uses_fieldkit_sf_frontmatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run must be called with 'fieldkit sf frontmatter --validate'."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme/pursuits/deal.md"])

    import hooks.validate_pursuit as mod

    with patch("subprocess.run", return_value=_clean_proc()) as mock_run:
        rc = mod.main()

    assert rc == 0
    # Two calls: --validate then --quality-check
    assert mock_run.call_count == 2

    first_call_args = mock_run.call_args_list[0][0][0]  # positional list arg
    # Must contain 'sf' and 'frontmatter' — either via binary or -m fieldkit
    assert "sf" in first_call_args
    assert "frontmatter" in first_call_args
    assert "--validate" in first_call_args
    # Must NOT reference the dead sf_pipeline module
    assert "sf_pipeline" not in first_call_args


def test_validate_pursuit_hook_command_quality_check_call_uses_fieldkit_sf_frontmatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.run second call must use 'fieldkit sf frontmatter --quality-check'."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme/pursuits/deal.md"])

    import hooks.validate_pursuit as mod

    with patch("subprocess.run", return_value=_clean_proc()) as mock_run:
        mod.main()

    second_call_args = mock_run.call_args_list[1][0][0]
    assert "sf" in second_call_args
    assert "frontmatter" in second_call_args
    assert "--quality-check" in second_call_args
    assert "sf_pipeline" not in second_call_args


def test_validate_pursuit_hook_command_fieldkit_binary_used_when_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When shutil.which('fieldkit') returns a path, use that binary."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme/pursuits/deal.md"])

    import hooks.validate_pursuit as mod

    # Patch _fieldkit_bin and _cmd_base directly on the module
    monkeypatch.setattr(mod, "_fieldkit_bin", "/usr/local/bin/fieldkit")
    monkeypatch.setattr(mod, "_cmd_base", ["/usr/local/bin/fieldkit", "sf", "frontmatter"])

    with patch("subprocess.run", return_value=_clean_proc()) as mock_run:
        rc = mod.main()

    assert rc == 0
    first_args = mock_run.call_args_list[0][0][0]
    assert first_args[0] == "/usr/local/bin/fieldkit"
    assert "sf" in first_args
    assert "frontmatter" in first_args


def test_validate_pursuit_hook_command_fallback_to_sys_executable_when_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fieldkit binary is not on PATH, fall back to sys.executable -m fieldkit."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme/pursuits/deal.md"])

    import hooks.validate_pursuit as mod

    # Simulate fieldkit not found on PATH
    monkeypatch.setattr(mod, "_fieldkit_bin", None)
    monkeypatch.setattr(mod, "_cmd_base", [sys.executable, "-m", "fieldkit", "sf", "frontmatter"])

    with patch("subprocess.run", return_value=_clean_proc()) as mock_run:
        rc = mod.main()

    assert rc == 0
    first_args = mock_run.call_args_list[0][0][0]
    assert sys.executable in first_args
    assert "-m" in first_args
    assert "fieldkit" in first_args
    assert "sf" in first_args
    assert "frontmatter" in first_args


def test_validate_pursuit_hook_command_exits_0_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook always exits 0 — it is advisory only."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme/pursuits/deal.md"])

    import hooks.validate_pursuit as mod

    with patch("subprocess.run", return_value=_clean_proc()):
        rc = mod.main()

    assert rc == 0


def test_validate_pursuit_hook_command_file_arg_passed_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file path from sys.argv must be forwarded to subprocess.run."""
    file_path = "accounts/acme/pursuits/deal.md"
    monkeypatch.setattr(sys, "argv", ["vp.py", file_path])

    import hooks.validate_pursuit as mod

    with patch("subprocess.run", return_value=_clean_proc()) as mock_run:
        mod.main()

    first_args = mock_run.call_args_list[0][0][0]
    assert "--file" in first_args
    assert file_path in first_args
