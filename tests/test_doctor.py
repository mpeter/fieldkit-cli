"""Tests for fieldkit.commands.doctor — consolidated health checks (D6).

Merges the old test_setup_verify.py (Google OAuth / SF cookie / slack-token
checks, load_env) and test_gmail_doctor.py (gmail.db integrity) suites
against the new fieldkit.commands.doctor.* modules.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import fieldkit.config._loader
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.commands.doctor.cli import _configuration_state, cli
from fieldkit.commands.doctor.gmail import check_gmail, doctor_gmail_cmd
from fieldkit.commands.doctor.google import _load_env, check_google, doctor_google_cmd
from fieldkit.commands.doctor.sf import check_sf, doctor_sf_cmd
from fieldkit.commands.doctor.shadowbot import check_shadowbot, doctor_shadowbot_cmd
from fieldkit.config import ConfigError
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("config_text", ["[malformed", "- list-item\n"])
def test_doctor_reports_unreadable_configuration_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_path)
    fieldkit.config._loader.clear_config_caches()
    results = [
        DoctorResult("sf", False, False, "not configured"),
        DoctorResult("gmail", False, False, "not configured"),
        DoctorResult("google", False, False, "not configured"),
        DoctorResult("shadowbot", False, False, "not configured"),
    ]

    try:
        with patch("fieldkit.commands.doctor.cli._run_all", return_value=results):
            result = CliRunner().invoke(cli, ["--json"])
    finally:
        fieldkit.config._loader.clear_config_caches()

    payload = json.loads(result.output)
    assert result.exit_code == 3
    assert {entry["configuration_state"] for entry in payload if entry["service"] != "gmail"} == {"invalid"}


def test_doctor_reports_invalid_configuration_even_when_runtime_checks_are_healthy() -> None:
    """Corrupt configuration takes precedence over leftover healthy runtime state."""
    results = [
        DoctorResult("sf", True, True, "session active"),
        DoctorResult("gmail", True, True, "cache healthy"),
        DoctorResult("google", True, True, "token valid"),
        DoctorResult("shadowbot", True, True, "token valid"),
    ]

    with (
        patch("fieldkit.commands.doctor.cli._run_all", return_value=results),
        patch(
            "fieldkit.commands.doctor.cli._configuration_state",
            side_effect=lambda service: "disabled" if service == "gmail" else "invalid",
        ),
    ):
        result = CliRunner().invoke(cli, ["--json"])

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert {entry["configuration_state"] for entry in payload if entry["service"] != "gmail"} == {"invalid"}


# ---------------------------------------------------------------------------
# doctor sf
# ---------------------------------------------------------------------------


def test_check_sf_no_cookie_file_not_configured(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    with patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file):
        result = check_sf()
    assert result.healthy is False
    assert result.configured is False


def test_check_sf_live_session_active_healthy(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text('{"sid": "fake"}', encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.check_sf_session", return_value=(True, "instance: example.com")),
    ):
        result = check_sf()
    assert result.healthy is True
    assert result.configured is True
    assert "instance:" in result.message


def test_check_sf_live_session_expired_unhealthy(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text('{"sid": "fake"}', encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.check_sf_session", return_value=(False, "SF session expired")),
    ):
        result = check_sf()
    assert result.healthy is False
    assert result.configured is True
    assert "auth sf" in result.message


def test_doctor_sf_cmd_exits_zero_when_healthy(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text('{"sid": "fake"}', encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.check_sf_session", return_value=(True, "instance: example.com")),
    ):
        result = CliRunner().invoke(doctor_sf_cmd, [])
    assert result.exit_code == 0, result.output


def test_doctor_sf_cmd_exits_two_when_not_configured(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    with patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file):
        result = CliRunner().invoke(doctor_sf_cmd, [])
    assert result.exit_code == 2, result.output


def test_doctor_sf_cmd_json_output(tmp_path: Path) -> None:
    cookie_file = tmp_path / "sf-cookies.json"
    with patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file):
        result = CliRunner().invoke(doctor_sf_cmd, ["--json"])
    payload = json.loads(result.output)
    assert payload["service"] == "sf"
    assert payload["configured"] is False


# ---------------------------------------------------------------------------
# doctor gmail
# ---------------------------------------------------------------------------


def _make_db(
    tmp_path: Path, *, with_messages: bool = True, with_people: bool = True, with_thread_accounts: bool = True
) -> Path:
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(db_path)
    if with_messages:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO messages VALUES (1, 'hello')")
    if with_people:
        conn.execute("CREATE TABLE people (email TEXT PRIMARY KEY, display_name TEXT)")
    if with_thread_accounts:
        conn.execute("CREATE TABLE thread_accounts (thread_id TEXT, account TEXT)")
    conn.commit()
    conn.close()
    return db_path


def test_check_gmail_missing_db_not_configured(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert result.configured is False


def test_check_gmail_all_tables_present_healthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is True
    assert result.configured is True


def test_check_gmail_missing_people_table_unhealthy(tmp_path: Path) -> None:
    """historic regression: missing people table → unhealthy with repair hint."""
    db_path = _make_db(tmp_path, with_people=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert "people" in result.message


def test_check_gmail_missing_thread_accounts_unhealthy(tmp_path: Path) -> None:
    """historic regression: missing thread_accounts table → unhealthy with repair hint."""
    db_path = _make_db(tmp_path, with_thread_accounts=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert "thread_accounts" in result.message


def test_check_gmail_repair_hint_mentions_sync(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_people=False, with_thread_accounts=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert "fieldkit sync" in result.message.lower()


def test_doctor_gmail_cmd_exits_zero_when_healthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(doctor_gmail_cmd, [])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_gmail_cmd_exits_two_when_unhealthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_people=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(doctor_gmail_cmd, [])
    assert result.exit_code == 2


def test_check_gmail_uses_explicit_database_without_resolving_default(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", side_effect=AssertionError("default resolved")):
        result = check_gmail(db_path)

    assert result.healthy is True
    assert result.configured is True


def test_check_gmail_explicit_database_retains_structure_checks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_thread_accounts=False)
    result = check_gmail(db_path)

    assert result.healthy is False
    assert "thread_accounts" in result.message


def test_doctor_gmail_cmd_db_selects_healthy_database(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", side_effect=AssertionError("default resolved")):
        result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_gmail_cmd_db_encodes_uri_metacharacters(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    selected_path = tmp_path / "gmail?cache=#shared.db"
    db_path.replace(selected_path)

    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(selected_path)])

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert not (tmp_path / "gmail").exists()


def test_doctor_gmail_cmd_db_missing_path_is_doctor_failure(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.db"
    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(missing_path)])

    assert result.exit_code == 2
    assert "not configured" in result.output
    assert "No such option" not in result.output


def test_doctor_gmail_cmd_db_preserves_json_contract(tmp_path: Path) -> None:
    import json

    db_path = _make_db(tmp_path)
    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(db_path), "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {
        "service": "gmail",
        "healthy": True,
        "configured": True,
        "message": "1 messages, 0.0 MB",
    }


# ---------------------------------------------------------------------------
# doctor google
# ---------------------------------------------------------------------------


def test_load_env_sets_environment_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("TEST_VAR_ALPHA=hello\nTEST_VAR_BETA=world\n", encoding="utf-8")
    monkeypatch.delenv("TEST_VAR_ALPHA", raising=False)
    monkeypatch.delenv("TEST_VAR_BETA", raising=False)

    _load_env(env_file)

    assert os.environ.get("TEST_VAR_ALPHA") == "hello"
    assert os.environ.get("TEST_VAR_BETA") == "world"


def test_load_env_skips_existing_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_VAR=from_file\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING_VAR", "pre_existing")

    _load_env(env_file)

    assert os.environ.get("EXISTING_VAR") == "pre_existing"


def test_load_env_handles_export_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("export SHELL_VAR=shell_value\n", encoding="utf-8")
    monkeypatch.delenv("SHELL_VAR", raising=False)

    _load_env(env_file)

    assert os.environ.get("SHELL_VAR") == "shell_value"


def test_load_env_missing_file_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.env"
    _load_env(missing)  # must not raise


def test_check_google_valid_token_healthy(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
    ):
        result = check_google()

    assert result.healthy is True
    assert result.configured is True
    assert "token valid" in result.message


def test_check_google_expired_refreshable_token_refreshes_and_passes(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "tok"
    mock_creds.to_json.return_value = "{}"

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch("fieldkit.google_oauth._refresh_request") as mock_request,
    ):
        result = check_google()

    assert result.healthy is True
    assert "refreshed" in result.message
    assert token_file.read_text(encoding="utf-8") == "{}"
    mock_creds.refresh.assert_called_once_with(mock_request.return_value)


def test_check_google_expired_no_refresh_token_unhealthy(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = None

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
    ):
        result = check_google()

    assert result.healthy is False
    assert "no refresh token" in result.message


def test_check_google_refresh_raises_unhealthy_with_exception_message(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "tok"
    mock_creds.refresh.side_effect = Exception("network error")

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch("google.auth.transport.requests.Request"),
    ):
        result = check_google()

    assert result.healthy is False
    assert "network error" in result.message


def test_check_google_no_token_credentials_in_env_configured_not_healthy(tmp_path: Path) -> None:
    """Token absent, credentials present → configured but not yet healthy (no token minted)."""
    token_path = tmp_path / "no-token.json"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_OAUTH_CLIENT_ID=test-client-id\nGOOGLE_OAUTH_CLIENT_SECRET=test-client-secret\n",
        encoding="utf-8",
    )

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_path),
        patch("fieldkit.commands.doctor.google.get_fieldkit_home", return_value=tmp_path),
        patch.dict("os.environ", {}, clear=True),
    ):
        result = check_google()

    assert result.healthy is False
    assert result.configured is True


def test_check_google_no_token_no_credentials_not_configured(tmp_path: Path) -> None:
    token_path = tmp_path / "no-token.json"

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_path),
        patch("fieldkit.commands.doctor.google.get_fieldkit_home", return_value=tmp_path),
        patch.dict("os.environ", {}, clear=True),
    ):
        result = check_google()

    assert result.healthy is False
    assert result.configured is False


def test_check_google_uses_current_directory_env_when_home_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing configuration root does not prevent credential diagnosis."""
    token_path = tmp_path / "no-token.json"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_OAUTH_CLIENT_ID=test-client-id\nGOOGLE_OAUTH_CLIENT_SECRET=test-client-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_path),
        patch("fieldkit.commands.doctor.google.get_fieldkit_home", side_effect=ConfigError("missing home")),
        patch.dict("os.environ", {}, clear=True),
    ):
        result = check_google()

    assert result.healthy is False
    assert result.configured is True


def test_doctor_google_cmd_exits_zero_when_healthy(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
    ):
        result = CliRunner().invoke(doctor_google_cmd, [])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# doctor shadowbot
# ---------------------------------------------------------------------------


def test_check_shadowbot_no_token_file_not_configured(tmp_path: Path) -> None:
    token_path = tmp_path / "shadowbot-token.json"
    with patch("fieldkit.commands.doctor.shadowbot.get_token_path", return_value=token_path):
        result = check_shadowbot()
    assert result.healthy is False
    assert result.configured is False


def test_check_shadowbot_live_token_resolves_healthy(tmp_path: Path) -> None:
    token_path = tmp_path / "shadowbot-token.json"
    token_path.write_text('{"refresh_token": "rt"}', encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.shadowbot.get_token_path", return_value=token_path),
        patch("fieldkit.commands.doctor.shadowbot.get_token", return_value="access-token"),
    ):
        result = check_shadowbot()
    assert result.healthy is True
    assert result.configured is True


def test_check_shadowbot_auth_error_unhealthy(tmp_path: Path) -> None:
    token_path = tmp_path / "shadowbot-token.json"
    token_path.write_text('{"refresh_token": "rt"}', encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.shadowbot.get_token_path", return_value=token_path),
        patch(
            "fieldkit.commands.doctor.shadowbot.get_token",
            side_effect=ShadowbotAuthError("No refresh token in token file."),
        ),
    ):
        result = check_shadowbot()
    assert result.healthy is False
    assert result.configured is True
    assert "auth shadowbot" in result.message


def test_doctor_shadowbot_cmd_exits_two_when_not_configured(tmp_path: Path) -> None:
    token_path = tmp_path / "shadowbot-token.json"
    with patch("fieldkit.commands.doctor.shadowbot.get_token_path", return_value=token_path):
        result = CliRunner().invoke(doctor_shadowbot_cmd, [])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# doctor (bare) — runs all services
# ---------------------------------------------------------------------------


def test_doctor_bare_all_healthy_exits_zero(tmp_path: Path) -> None:
    with (
        patch("fieldkit.commands.doctor.cli.check_sf") as mock_sf,
        patch("fieldkit.commands.doctor.cli.check_gmail") as mock_gmail,
        patch("fieldkit.commands.doctor.cli.check_google") as mock_google,
        patch("fieldkit.commands.doctor.cli.check_shadowbot") as mock_shadowbot,
    ):
        from fieldkit.commands.doctor._result import DoctorResult

        mock_sf.return_value = DoctorResult("sf", True, True, "ok")
        mock_gmail.return_value = DoctorResult("gmail", True, True, "ok")
        mock_google.return_value = DoctorResult("google", True, True, "ok")
        mock_shadowbot.return_value = DoctorResult("shadowbot", True, True, "ok")

        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 0, result.output


def test_doctor_bare_disabled_integration_is_informational(tmp_path: Path) -> None:
    with (
        patch("fieldkit.commands.doctor.cli.check_sf") as mock_sf,
        patch("fieldkit.commands.doctor.cli.check_gmail") as mock_gmail,
        patch("fieldkit.commands.doctor.cli.check_google") as mock_google,
        patch("fieldkit.commands.doctor.cli.check_shadowbot") as mock_shadowbot,
        patch("fieldkit.commands.doctor.cli._configuration_state", return_value="disabled"),
    ):
        from fieldkit.commands.doctor._result import DoctorResult

        mock_sf.return_value = DoctorResult("sf", False, False, "run 'fieldkit auth sf'")
        mock_gmail.return_value = DoctorResult("gmail", True, True, "ok")
        mock_google.return_value = DoctorResult("google", True, True, "ok")
        mock_shadowbot.return_value = DoctorResult("shadowbot", True, True, "ok")

        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 0, result.output
    assert "sf: optional — not configured" in result.output


def test_doctor_bare_enabled_integration_without_auth_exits_two() -> None:
    with (
        patch("fieldkit.commands.doctor.cli.check_sf") as mock_sf,
        patch("fieldkit.commands.doctor.cli.check_gmail") as mock_gmail,
        patch("fieldkit.commands.doctor.cli.check_google") as mock_google,
        patch("fieldkit.commands.doctor.cli.check_shadowbot") as mock_shadowbot,
        patch(
            "fieldkit.commands.doctor.cli._configuration_state",
            side_effect=lambda service: "enabled" if service == "sf" else "disabled",
        ),
    ):
        from fieldkit.commands.doctor._result import DoctorResult

        mock_sf.return_value = DoctorResult("sf", False, False, "run 'fieldkit auth sf'")
        mock_gmail.return_value = DoctorResult("gmail", False, False, "optional")
        mock_google.return_value = DoctorResult("google", False, False, "optional")
        mock_shadowbot.return_value = DoctorResult("shadowbot", False, False, "optional")

        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 2, result.output


def test_explicit_enablement_reads_service_configuration() -> None:
    with patch("fieldkit.commands.doctor.cli.get_integration_configuration_state", return_value="enabled"):
        sf_state = _configuration_state("sf")
    with patch("fieldkit.commands.doctor.cli.get_integration_configuration_state", return_value="enabled"):
        shadowbot_state = _configuration_state("shadowbot")

    assert sf_state == "enabled"
    assert shadowbot_state == "enabled"


def test_absent_shadowbot_configuration_is_disabled() -> None:
    with patch("fieldkit.commands.doctor.cli.get_integration_configuration_state", return_value="disabled"):
        result = _configuration_state("shadowbot")

    assert result == "disabled"


def test_doctor_bare_json_output(tmp_path: Path) -> None:
    with (
        patch("fieldkit.commands.doctor.cli.check_sf") as mock_sf,
        patch("fieldkit.commands.doctor.cli.check_gmail") as mock_gmail,
        patch("fieldkit.commands.doctor.cli.check_google") as mock_google,
        patch("fieldkit.commands.doctor.cli.check_shadowbot") as mock_shadowbot,
    ):
        from fieldkit.commands.doctor._result import DoctorResult

        mock_sf.return_value = DoctorResult("sf", True, True, "ok")
        mock_gmail.return_value = DoctorResult("gmail", True, True, "ok")
        mock_google.return_value = DoctorResult("google", True, True, "ok")
        mock_shadowbot.return_value = DoctorResult("shadowbot", True, True, "ok")

        result = CliRunner().invoke(cli, ["--json"])

    payload = json.loads(result.output)
    assert {entry["service"] for entry in payload} == {"sf", "gmail", "google", "shadowbot"}
    assert all(entry["enabled"] is True for entry in payload)


def test_doctor_bare_dispatches_to_subcommand(tmp_path: Path) -> None:
    """Bare `doctor sf` must run only the sf check, not all services."""
    cookie_file = tmp_path / "sf-cookies.json"
    with patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file):
        result = CliRunner().invoke(cli, ["sf"])
    assert "sf:" in result.output
    assert "gmail:" not in result.output
