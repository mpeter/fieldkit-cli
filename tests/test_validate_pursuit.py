"""Tests for tools/hooks/validate_pursuit.py — advisory frontmatter validation hook.

Covers:
- No-arg early return
- Non-pursuit path skip
- Pursuit path triggers validation subprocess
- Errors surfaced to stderr (advisory only — always exits 0)
- Quality-check branch
- Exception swallowing (subprocess failure)
"""

import sys
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# Patch sys.argv before importing so the module-level sys.argv[1] guard doesn't fire


def _load_main():
    """Import validate_pursuit.main() fresh each time."""
    import hooks.validate_pursuit as mod

    return mod.main


# ── TestValidatePursuit (flattened) ─────────────────────────────────────────


def test_validate_pursuit_no_arg_returns_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hooks/validate_pursuit.py"])
    import hooks.validate_pursuit as mod

    rc = mod.main()
    assert rc == 0


def test_validate_pursuit_non_pursuit_path_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Files outside accounts/.../pursuits/ are silently skipped."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["hooks/validate_pursuit.py", "accounts/acme-bank/meetings/notes.md"],
    )
    import hooks.validate_pursuit as mod

    with patch("subprocess.run") as mock_run:
        rc = mod.main()
    assert rc == 0
    mock_run.assert_not_called()


def test_validate_pursuit_non_markdown_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hooks/validate_pursuit.py", "some/other/file.py"])
    import hooks.validate_pursuit as mod

    with patch("subprocess.run") as mock_run:
        rc = mod.main()
    assert rc == 0
    mock_run.assert_not_called()


def _validate_pursuit_make_proc(stderr: str = "", stdout: str = "", returncode: int = 0) -> CompletedProcess:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_validate_pursuit_pursuit_path_calls_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme-bank/pursuits/ads.md"])
    import hooks.validate_pursuit as mod

    clean = _validate_pursuit_make_proc()
    with patch("subprocess.run", return_value=clean) as mock_run:
        rc = mod.main()
    assert rc == 0
    assert mock_run.call_count == 2  # validate + quality-check


def test_validate_pursuit_no_errors_silent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/globalpay/pursuits/deal.md"])
    import hooks.validate_pursuit as mod

    with patch("subprocess.run", return_value=_validate_pursuit_make_proc()):
        mod.main()
    err = capsys.readouterr().err
    assert err == ""


def test_validate_pursuit_validation_errors_printed_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme-bank/pursuits/ads.md"])
    import hooks.validate_pursuit as mod

    err_proc = _validate_pursuit_make_proc(stderr="SCHEMA ERROR: field missing")
    clean = _validate_pursuit_make_proc()
    with patch("subprocess.run", side_effect=[err_proc, clean]):
        rc = mod.main()
    assert rc == 0  # always 0 — advisory only
    err = capsys.readouterr().err
    assert "FRONTMATTER" in err
    assert "SCHEMA ERROR" in err


def test_validate_pursuit_warning_lines_filtered_from_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Lines starting with WARNING: are filtered out — only real errors surface."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme-bank/pursuits/ads.md"])
    import hooks.validate_pursuit as mod

    err_proc = _validate_pursuit_make_proc(stderr="WARNING: schema not found\nSCHEMA ERROR: real error")
    clean = _validate_pursuit_make_proc()
    with patch("subprocess.run", side_effect=[err_proc, clean]):
        mod.main()
    err = capsys.readouterr().err
    assert "WARNING" not in err
    assert "real error" in err


def test_validate_pursuit_warning_only_stderr_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """If all stderr lines are WARNING:, nothing is printed."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme-bank/pursuits/ads.md"])
    import hooks.validate_pursuit as mod

    warn_only = _validate_pursuit_make_proc(stderr="WARNING: schema not found")
    clean = _validate_pursuit_make_proc()
    with patch("subprocess.run", side_effect=[warn_only, clean]):
        mod.main()
    err = capsys.readouterr().err
    assert "FRONTMATTER" not in err


def test_validate_pursuit_quality_output_printed_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/acme-bank/pursuits/ads.md"])
    import hooks.validate_pursuit as mod

    clean = _validate_pursuit_make_proc()
    quality = _validate_pursuit_make_proc(stdout="MEDDPICC gap: no champion", stderr="")
    with patch("subprocess.run", side_effect=[clean, quality]):
        rc = mod.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "QUALITY" in err
    assert "MEDDPICC gap" in err


def test_validate_pursuit_subprocess_exception_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """If subprocess.run raises, errors are swallowed and rc is still 0."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/globalpay/pursuits/x.md"])
    import hooks.validate_pursuit as mod

    with patch("subprocess.run", side_effect=OSError("not found")):
        rc = mod.main()
    assert rc == 0


def test_validate_pursuit_always_exits_0_even_with_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_pursuit is advisory — never blocks with non-zero exit."""
    monkeypatch.setattr(sys, "argv", ["vp.py", "accounts/midwest-ins/pursuits/rhel.md"])
    import hooks.validate_pursuit as mod

    err_proc = _validate_pursuit_make_proc(stderr="SCHEMA ERROR: everything is broken")
    with patch("subprocess.run", return_value=err_proc):
        rc = mod.main()
    assert rc == 0
