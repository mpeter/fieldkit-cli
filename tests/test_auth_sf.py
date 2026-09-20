"""Tests for fieldkit.commands.auth.sf — direct-write and interactive-prompt paths.

Merges the old test_setup_inject_sid.py (--sid direct write) and
test_setup_sf_cookies.py (interactive prompt + session validation) suites
against the consolidated fieldkit.commands.auth.sf module.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.auth.sf import (
    _build_sid_cookie,
    _validate_sid_format,
    _write_sid_cookie,
    auth_sf_cmd,
)

pytestmark = pytest.mark.unit


def _sid_file(tmp_path: Path, sid: str) -> Path:
    """Create one owner-only SID input file for command tests."""
    path = tmp_path / "sid"
    path.write_text(sid, encoding="utf-8")
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# _validate_sid_format
# ---------------------------------------------------------------------------


def test_validate_sid_format_valid_sf_sid() -> None:
    sid = "00D5e000001eBaD!AQEAQLSome_long_session_id_value"
    assert _validate_sid_format(sid) is True


def test_validate_sid_format_empty_string() -> None:
    assert _validate_sid_format("") is False


def test_validate_sid_format_no_exclamation() -> None:
    assert _validate_sid_format("00D5e000001eBaDshortvalue") is False


def test_validate_sid_format_too_short() -> None:
    assert _validate_sid_format("abc!def") is False


# ---------------------------------------------------------------------------
# _build_sid_cookie
# ---------------------------------------------------------------------------


def test_build_sid_cookie_builds_correct_dict() -> None:
    cookie = _build_sid_cookie("MYSID123", "examplecrm.my.salesforce.com")
    assert cookie["name"] == "sid"
    assert cookie["value"] == "MYSID123"
    assert cookie["domain"] == ".examplecrm.my.salesforce.com"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is True


def test_build_sid_cookie_domain_has_leading_dot() -> None:
    cookie = _build_sid_cookie("SID", "org.my.salesforce.com")
    assert str(cookie["domain"]).startswith(".")


# ---------------------------------------------------------------------------
# _write_sid_cookie
# ---------------------------------------------------------------------------


def test_write_sid_cookie_creates_cookie_file_fresh(tmp_path: Path) -> None:
    """Creates a new cookie file when none exists."""
    cookie_file = tmp_path / "cookies.json"
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
    ):
        _write_sid_cookie("MY_SID!abc")

    assert cookie_file.exists()
    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    cookies = data["cookies"]
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sid"
    assert cookies[0]["value"] == "MY_SID!abc"


def test_write_sid_cookie_file_permissions_are_600(tmp_path: Path) -> None:
    """Cookie file is written with 0o600 permissions."""
    cookie_file = tmp_path / "cookies.json"
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
    ):
        _write_sid_cookie("SID!value")

    assert oct(cookie_file.stat().st_mode & 0o777) == oct(0o600)


def test_write_sid_cookie_updates_existing_sid_cookie(tmp_path: Path) -> None:
    """Updates existing sid cookie in an existing cookie file."""
    cookie_file = tmp_path / "cookies.json"
    existing = {
        "cookies": [
            {
                "name": "sid",
                "value": "OLD_SID",
                "domain": ".org.my.salesforce.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            }
        ]
    }
    cookie_file.write_text(json.dumps(existing), encoding="utf-8")

    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
    ):
        _write_sid_cookie("NEW_SID!abc")

    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    sid_cookies = [c for c in data["cookies"] if c["name"] == "sid"]
    assert len(sid_cookies) == 1
    assert sid_cookies[0]["value"] == "NEW_SID!abc"


def test_write_sid_cookie_preserves_other_cookies(tmp_path: Path) -> None:
    """Non-sid cookies in the existing file are preserved."""
    cookie_file = tmp_path / "cookies.json"
    existing = {
        "cookies": [
            {"name": "csrftoken", "value": "CSRF", "domain": ".my.salesforce.com", "path": "/"},
            {"name": "sid", "value": "OLD", "domain": ".my.salesforce.com", "path": "/"},
        ]
    }
    cookie_file.write_text(json.dumps(existing), encoding="utf-8")

    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
    ):
        _write_sid_cookie("NEW!abc")

    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    names = {c["name"] for c in data["cookies"]}
    assert "csrftoken" in names
    assert "sid" in names


def test_write_sid_cookie_raises_config_error_when_no_sf_url(tmp_path: Path) -> None:
    """_write_sid_cookie raises ConfigError when get_sf_rest_base_url returns empty string."""
    from fieldkit.config._loader import ConfigError

    cookie_file = tmp_path / "sf-cookies.json"
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value=""),
        pytest.raises(ConfigError, match=r"salesforce\.org_url|sf_org_url"),
    ):
        _write_sid_cookie("SID!value")


# ---------------------------------------------------------------------------
# auth_sf_cmd — direct-write path (--sid)
# ---------------------------------------------------------------------------


def test_auth_sf_cmd_help_exits_0() -> None:
    runner = CliRunner()
    result = runner.invoke(auth_sf_cmd, ["--help"])
    assert result.exit_code == 0


def test_auth_sf_cmd_json_status_is_credential_safe() -> None:
    runner = CliRunner()
    result_value = MagicMock(service="sf", healthy=False, configured=True, message="expired")
    with patch("fieldkit.commands.doctor.sf.check_sf", return_value=result_value):
        result = runner.invoke(auth_sf_cmd, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "authenticated": False,
        "configured": True,
        "service": "sf",
        "state": "reauthorization-required",
    }


def test_auth_sf_cmd_direct_sid_writes_cookie_and_exits_0(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()
    session_check = MagicMock()
    session_check.check_sf_session.return_value = (True, "session valid")
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": session_check}),
    ):
        result = runner.invoke(auth_sf_cmd, ["--sid-file", str(_sid_file(tmp_path, "00D5e000001eBaD!AQEAQLvalidsid"))])

    assert result.exit_code == 0, result.output
    assert cookie_file.exists()
    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    assert data["cookies"][0]["value"] == "00D5e000001eBaD!AQEAQLvalidsid"


def test_auth_sf_cmd_direct_sid_warns_on_invalid_format(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()
    session_check = MagicMock()
    session_check.check_sf_session.return_value = (True, "session valid")
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": session_check}),
    ):
        result = runner.invoke(auth_sf_cmd, ["--sid-file", str(_sid_file(tmp_path, "not-a-valid-sid"))])

    assert result.exit_code == 0
    assert "WARNING" in result.output


def test_auth_sf_cmd_direct_sid_rejects_expired_session(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()
    session_check = MagicMock()
    session_check.check_sf_session.return_value = (False, "401 Unauthorized")
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": session_check}),
    ):
        result = runner.invoke(auth_sf_cmd, ["--sid-file", str(_sid_file(tmp_path, "00D5e000001eBaD!AQEAQLvalidsid"))])

    assert result.exit_code == 2
    assert "session check failed" in result.output


def test_auth_sf_cmd_direct_sid_restores_existing_cookie_when_validation_fails(tmp_path: Path) -> None:
    """An invalid injected session cannot replace a working saved session."""
    cookie_file = tmp_path / "cookies.json"
    original = b'{"cookies": [{"name": "sid", "value": "working-session"}]}'
    cookie_file.write_bytes(original)
    runner = CliRunner()
    session_check = MagicMock()
    session_check.check_sf_session.return_value = (False, "401 Unauthorized")

    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": session_check}),
    ):
        result = runner.invoke(
            auth_sf_cmd, ["--sid-file", str(_sid_file(tmp_path, "00D5e000001eBaD!AQEAQLexpiredsid"))]
        )

    assert result.exit_code == 2
    assert cookie_file.read_bytes() == original


def test_auth_sf_cmd_direct_sid_removes_new_cookie_when_validation_fails(tmp_path: Path) -> None:
    """A failed first-time injection leaves no unusable credential behind."""
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()
    session_check = MagicMock()
    session_check.check_sf_session.return_value = (False, "401 Unauthorized")

    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": session_check}),
    ):
        result = runner.invoke(
            auth_sf_cmd, ["--sid-file", str(_sid_file(tmp_path, "00D5e000001eBaD!AQEAQLexpiredsid"))]
        )

    assert result.exit_code == 2
    assert not cookie_file.exists()


# ---------------------------------------------------------------------------
# auth_sf_cmd — interactive-prompt path (no --sid)
# ---------------------------------------------------------------------------


def test_auth_sf_cmd_cancel_on_prompt_exits_0(tmp_path: Path) -> None:
    """historic regression: Ctrl+C during the prompt is a clean cancel — exit 0."""
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()
    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.commands.auth.sf.get_salesforce_org_url", return_value=""),
        patch("fieldkit.commands.auth.sf.stdin_is_interactive", return_value=True),
        patch("fieldkit.commands.auth.sf._prompt_for_sid", side_effect=SystemExit(0)),
    ):
        result = runner.invoke(auth_sf_cmd, catch_exceptions=False)

    assert result.exit_code == 0


def test_auth_sf_cmd_noninteractive_reauthorization_does_not_prompt_or_write() -> None:
    """A piped invocation gives recovery guidance before any credential mutation."""
    runner = CliRunner()

    with (
        patch("fieldkit.commands.auth.sf.stdin_is_interactive", return_value=False),
        patch("fieldkit.commands.auth.sf._prompt_for_sid") as prompt,
        patch("fieldkit.commands.auth.sf._write_sid_cookie") as write,
    ):
        result = runner.invoke(auth_sf_cmd, [], catch_exceptions=False)

    assert result.exit_code == 2
    assert "fieldkit auth sf" in result.stderr
    prompt.assert_not_called()
    write.assert_not_called()


def test_auth_sf_cmd_bad_session_exits_2(tmp_path: Path) -> None:
    """When check_sf_session fails, exits 2 with error message."""
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()

    mock_session_check_mod = MagicMock()
    mock_session_check_mod.check_sf_session.return_value = (False, "401 Unauthorized")

    with (
        patch("fieldkit.commands.auth.sf._prompt_for_sid", return_value="00D!validformat"),
        patch("fieldkit.commands.auth.sf.stdin_is_interactive", return_value=True),
        patch("fieldkit.commands.auth.sf._write_sid_cookie"),
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": mock_session_check_mod}),
    ):
        result = runner.invoke(auth_sf_cmd)

    assert result.exit_code == 2


def test_auth_sf_cmd_happy_path_exits_0(tmp_path: Path) -> None:
    """Valid sid + passing session check -> exits 0, cookie file written."""
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()

    mock_session_check_mod = MagicMock()
    mock_session_check_mod.check_sf_session.return_value = (True, "Session valid")

    with (
        patch("fieldkit.commands.auth.sf._prompt_for_sid", return_value="00D5e!REALSID"),
        patch("fieldkit.commands.auth.sf.stdin_is_interactive", return_value=True),
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": mock_session_check_mod}),
    ):
        result = runner.invoke(auth_sf_cmd)

    assert result.exit_code == 0, result.output
    assert cookie_file.exists()


def test_auth_sf_cmd_instructions_mention_devtools(tmp_path: Path) -> None:
    """The instructions printed to the user mention DevTools."""
    cookie_file = tmp_path / "cookies.json"
    runner = CliRunner()

    mock_session_check_mod = MagicMock()
    mock_session_check_mod.check_sf_session.return_value = (True, "ok")

    with (
        patch("fieldkit.commands.auth.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.auth.sf.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.commands.auth.sf.get_salesforce_org_url", return_value=""),
        patch("fieldkit.commands.auth.sf.stdin_is_interactive", return_value=True),
        patch.dict("sys.modules", {"fieldkit.commands.sf.session_check": mock_session_check_mod}),
    ):
        result = runner.invoke(auth_sf_cmd, input="00D5e!REALSID\n")

    assert "DevTools" in result.output or "devtools" in result.output.lower()
