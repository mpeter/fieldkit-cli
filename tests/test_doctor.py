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
from fieldkit.__main__ import main
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.commands.doctor.cli import _configuration_state, _exit_code, cli
from fieldkit.commands.doctor.gmail import check_gmail, doctor_gmail_cmd
from fieldkit.commands.doctor.google import check_google, doctor_google_cmd
from fieldkit.commands.doctor.sf import check_sf, doctor_sf_cmd
from fieldkit.commands.doctor.shadowbot import check_shadowbot, doctor_shadowbot_cmd
from fieldkit.config import ConfigError
from fieldkit.config.dotenv import write_dotenv_file
from fieldkit.errors import GmailAuthError, GoogleCredentialRefreshRetryableError
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit


def test_unhealthy_doctor_result_requires_failure_category() -> None:
    with pytest.raises(ValueError, match="unhealthy results require one"):
        DoctorResult("gmail", False, True, "cache unavailable")


def test_healthy_doctor_result_rejects_failure_category() -> None:
    with pytest.raises(ValueError, match="healthy results must have no failure kind"):
        DoctorResult("gmail", True, True, "cache healthy", failure_kind="data")


def test_retryable_doctor_result_exits_partial() -> None:
    result = DoctorResult("gmail", False, True, "cache active", failure_kind="retryable")

    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("other_kind", "expected"),
    [(None, 1), ("retryable", 1), ("auth", 2), ("data", 3)],
)
def test_aggregate_doctor_preserves_failure_priority(other_kind: str | None, expected: int) -> None:
    retryable = DoctorResult("gmail", False, True, "cache active", failure_kind="retryable")
    other = DoctorResult("google", True, True, "healthy")
    if other_kind == "retryable":
        other = DoctorResult("google", False, True, "temporarily unavailable", failure_kind="retryable")
    elif other_kind == "auth":
        other = DoctorResult("google", False, True, "sign-in needed", failure_kind="auth")
    elif other_kind == "data":
        other = DoctorResult("google", False, True, "invalid data", failure_kind="data")

    result = _exit_code([retryable, other], {"gmail": "enabled", "google": "enabled"})

    assert result == expected


@pytest.mark.parametrize("config_text", ["[malformed", "- list-item\n"])
def test_doctor_reports_unreadable_configuration_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_path)
    fieldkit.config._loader.clear_config_caches()
    results = [
        DoctorResult("sf", False, False, "not configured", failure_kind="auth"),
        DoctorResult("gmail", False, False, "not configured", failure_kind="data"),
        DoctorResult("google", False, False, "not configured", failure_kind="auth"),
        DoctorResult("shadowbot", False, False, "not configured", failure_kind="auth"),
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
    if with_messages and with_people and with_thread_accounts:
        from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
        from fieldkit.sqlite_publication import SQLiteMutationConnection

        initialize_gmail_publication(db_path)

        def seed(connection: SQLiteMutationConnection) -> None:
            connection.execute(
                "INSERT INTO threads(thread_id, subject, message_count) VALUES (?, ?, ?)",
                ("thread-1", "Fictional fixture", 1),
            )
            connection.execute(
                """
                INSERT INTO messages(message_id, thread_id, body_plain, synced_at)
                VALUES (?, ?, ?, ?)
                """,
                ("message-1", "thread-1", "Fictional fixture", "2026-09-28T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
                (GMAIL_QUERY_READY_KEY,),
            )

        apply_gmail_page(db_path, seed)
        return db_path

    # Unsupported legacy shapes remain useful negative fixtures. The doctor
    # must reject them without attempting an in-place migration.
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


@pytest.mark.parametrize("state", ["missing", "empty", "corrupt", "missing-table"])
@pytest.mark.parametrize("as_json", [False, True])
def test_gmail_data_failures_are_non_destructive_data_errors(tmp_path: Path, state: str, as_json: bool) -> None:
    db_path = tmp_path / "gmail.db"
    if state == "empty":
        db_path.touch()
    elif state == "corrupt":
        db_path.write_bytes(b"not a SQLite database")
    elif state == "missing-table":
        db_path = _make_db(tmp_path, with_people=False)
    before = db_path.read_bytes() if db_path.exists() else None

    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(db_path), *(["--json"] if as_json else [])])

    assert result.exit_code == 3, result.output
    assert (db_path.read_bytes() if db_path.exists() else None) == before
    message = json.loads(result.output)["message"] if as_json else result.output
    assert "remove" not in message.lower()
    if state in {"empty", "corrupt"}:
        assert "preserve" in message.lower()
    if as_json:
        assert set(json.loads(result.output)) == {"service", "healthy", "configured", "message"}


def test_aggregate_doctor_reports_gmail_data_failure(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_people=False)
    with (
        patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path),
        patch("fieldkit.commands.doctor.cli.check_sf", return_value=DoctorResult("sf", True, True, "ok")),
        patch("fieldkit.commands.doctor.cli.check_google", return_value=DoctorResult("google", True, True, "ok")),
        patch("fieldkit.commands.doctor.cli.check_shadowbot", return_value=DoctorResult("shadowbot", True, True, "ok")),
        patch("fieldkit.commands.doctor.cli._configuration_state", return_value="enabled"),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 3, result.output


def test_gmail_stat_failure_is_a_preserved_data_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "gmail.db"

    def denied(_path: Path, *, follow_symlinks: bool = True) -> None:
        raise PermissionError("fixture access denied")

    with monkeypatch.context() as context:
        context.setattr(Path, "stat", denied)
        result = check_gmail(db_path)

    assert result.exit_code == 3
    assert result.configured is True
    assert "preserve" in result.message


def test_doctor_data_failure_takes_precedence_over_auth() -> None:
    results = [
        DoctorResult("gmail", False, True, "cache unreadable", failure_kind="data"),
        DoctorResult("sf", False, True, "session expired", failure_kind="auth"),
    ]

    assert _exit_code(results, {"gmail": "enabled", "sf": "enabled"}) == 3
    assert _exit_code(results, {"gmail": "disabled", "sf": "enabled"}) == 2
    assert _exit_code(results, {"gmail": "disabled", "sf": "disabled"}) == 0


def test_check_gmail_missing_db_not_configured(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert result.configured is False


def test_check_gmail_first_page_failure_is_retryable_not_healthy(tmp_path: Path) -> None:
    """A schema-only publication must not masquerade as a usable empty cache."""
    from fieldkit.gmail.publication import initialize_gmail_publication

    db_path = tmp_path / "gmail.db"
    initialize_gmail_publication(db_path)

    result = check_gmail(db_path)

    assert result.healthy is False
    assert result.configured is True
    assert result.failure_kind == "retryable"
    assert result.exit_code == 1
    assert "not ready" in result.message


def test_check_gmail_all_tables_present_healthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is True
    assert result.configured is True


def test_check_gmail_missing_people_table_unhealthy(tmp_path: Path) -> None:
    """An unmanaged partial schema is invalid rather than repaired in place."""
    db_path = _make_db(tmp_path, with_people=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert result.failure_kind == "data"
    assert "preserve" in result.message


def test_check_gmail_missing_thread_accounts_unhealthy(tmp_path: Path) -> None:
    """A legacy schema without account tags requires explicit recovery."""
    db_path = _make_db(tmp_path, with_thread_accounts=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert result.healthy is False
    assert result.failure_kind == "data"
    assert "preserve" in result.message


def test_check_gmail_repair_hint_mentions_sync(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_people=False, with_thread_accounts=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = check_gmail()
    assert "fieldkit gmail sync" in result.message.lower()


def test_doctor_gmail_cmd_exits_zero_when_healthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(doctor_gmail_cmd, [])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_gmail_cmd_exits_three_when_unhealthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_people=False)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(doctor_gmail_cmd, [])
    assert result.exit_code == 3


def test_check_gmail_uses_explicit_database_without_resolving_default(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", side_effect=AssertionError("default resolved")):
        result = check_gmail(db_path)

    assert result.healthy is True
    assert result.configured is True


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_gmail_doctor_reads_the_committed_generation_without_touching_source_sidecars(
    tmp_path: Path, suffix: str
) -> None:
    db_path = _make_db(tmp_path)
    sidecar = db_path.with_name(db_path.name + suffix)
    sidecar.write_bytes(b"unverified companion state")
    observed_paths = (db_path, sidecar)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in observed_paths}

    result = check_gmail(db_path)

    assert result.healthy is True
    assert result.exit_code == 0
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in observed_paths} == before


@pytest.mark.parametrize("state", ["empty", "corrupt"])
@pytest.mark.parametrize("as_json", [False, True])
def test_gmail_doctor_diagnostics_do_not_expose_selected_paths(tmp_path: Path, state: str, as_json: bool) -> None:
    db_path = tmp_path / "private-cache-marker.db"
    db_path.write_bytes(b"" if state == "empty" else b"corrupt SQLite data")
    argv = ["--db", str(db_path), *(["--json"] if as_json else [])]

    result = CliRunner().invoke(doctor_gmail_cmd, argv)

    assert result.exit_code == 3
    assert str(db_path) not in result.output
    assert db_path.name not in result.output
    assert "preserve" in result.output


def test_gmail_doctor_rejects_directory_without_exposing_path(tmp_path: Path) -> None:
    selected = tmp_path / "private-cache-marker"
    selected.mkdir()

    result = check_gmail(selected)

    assert result.healthy is False
    assert result.exit_code == 3
    assert str(selected) not in result.message
    assert selected.name not in result.message


@pytest.mark.parametrize("as_json", [False, True])
def test_gmail_doctor_configuration_failure_is_safe_at_public_dispatch(
    as_json: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "private-cache-resolution-marker"
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: False)
    with patch(
        "fieldkit.commands.doctor.gmail.get_gmail_db_path",
        side_effect=ConfigError(f"Invalid cache path: /fictional/{sentinel}/gmail.sqlite"),
    ):
        result = main(["doctor", "gmail", *(["--json"] if as_json else [])])

    assert result == 3
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err
    assert "preserve" in output.out
    if as_json:
        assert json.loads(output.out)["healthy"] is False


def test_check_gmail_explicit_database_rejects_unmanaged_partial_schema(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, with_thread_accounts=False)
    result = check_gmail(db_path)

    assert result.healthy is False
    assert result.failure_kind == "data"
    assert "preserve" in result.message


def test_doctor_gmail_cmd_db_selects_healthy_database(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with patch("fieldkit.commands.doctor.gmail.get_gmail_db_path", side_effect=AssertionError("default resolved")):
        result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_gmail_cmd_db_encodes_uri_metacharacters(tmp_path: Path) -> None:
    from fieldkit.gmail.publication import publication_root_for

    db_path = _make_db(tmp_path)
    selected_path = tmp_path / "gmail?cache=#shared.db"
    publication_root_for(db_path).replace(publication_root_for(selected_path))
    db_path.replace(selected_path)

    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(selected_path)])

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert not (tmp_path / "gmail").exists()


def test_doctor_gmail_cmd_db_missing_path_is_doctor_failure(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.db"
    result = CliRunner().invoke(doctor_gmail_cmd, ["--db", str(missing_path)])

    assert result.exit_code == 3
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
        "message": "1 messages, 0.1 MB",
    }


# ---------------------------------------------------------------------------
# doctor google
# ---------------------------------------------------------------------------


def test_check_google_loads_exact_workspace_dotenv_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    client_id = "client-${TOKEN}-'\"\\\\\nnext"
    client_secret = "secret-$TOKEN-\N{SNOWMAN}"
    write_dotenv_file(
        tmp_path / ".env",
        {
            "GOOGLE_OAUTH_CLIENT_ID": client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
        },
    )
    monkeypatch.setenv("TOKEN", "must-not-expand")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=tmp_path / "missing-token"),
        patch("fieldkit.commands.doctor.google.get_fieldkit_home", return_value=tmp_path),
    ):
        result = check_google()

    assert result.configured is True
    assert result.healthy is False
    assert os.environ["GOOGLE_OAUTH_CLIENT_ID"] == client_id
    assert os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] == client_secret


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


def test_check_google_refresh_retryable_failure_is_fixed_and_partial(tmp_path: Path) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "tok"

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch(
            "fieldkit.commands.doctor.google.refresh_google_credentials",
            side_effect=GoogleCredentialRefreshRetryableError("fictional-private-network-marker"),
        ),
    ):
        result = check_google()

    assert result.healthy is False
    assert result.failure_kind == "retryable"
    assert result.exit_code == 1
    assert result.message == "token refresh is temporarily unavailable — retry later"
    assert "fictional-private-network-marker" not in result.message


@pytest.mark.parametrize(
    ("failure", "expected_kind", "expected_exit", "expected_message"),
    [
        (
            GmailAuthError("fictional-private-auth-marker"),
            "auth",
            2,
            "token refresh was rejected — run 'fieldkit auth google'",
        ),
        (
            OSError("fictional-private-persistence-marker"),
            "data",
            3,
            "token file could not be updated safely",
        ),
    ],
)
def test_check_google_refresh_failures_have_typed_payload_free_results(
    tmp_path: Path,
    failure: Exception,
    expected_kind: str,
    expected_exit: int,
    expected_message: str,
) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock(expired=True, refresh_token="tok", valid=False)

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch("fieldkit.commands.doctor.google.refresh_google_credentials", side_effect=failure),
    ):
        result = check_google()

    assert result.failure_kind == expected_kind
    assert result.exit_code == expected_exit
    assert result.message == expected_message
    assert "fictional-private" not in result.message


@pytest.mark.parametrize("as_json", [False, True])
def test_doctor_google_malformed_token_is_payload_free_data_failure(tmp_path: Path, as_json: bool) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    marker = "fictional-private-path-and-token"

    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            side_effect=RuntimeError(marker),
        ),
    ):
        result = CliRunner().invoke(doctor_google_cmd, ["--json"] if as_json else [])

    assert result.exit_code == 3
    assert marker not in result.output
    if as_json:
        payload = json.loads(result.output)
        assert payload["message"] == "token file is invalid or unreadable"
    else:
        assert "token file is invalid or unreadable" in result.output


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_message"),
    [
        (
            GoogleCredentialRefreshRetryableError("fictional-private-retry-marker"),
            1,
            "token refresh is temporarily unavailable — retry later",
        ),
        (
            RuntimeError("fictional-private-runtime-marker"),
            1,
            "token refresh is temporarily unavailable — retry later",
        ),
        (
            GmailAuthError("fictional-private-auth-marker"),
            2,
            "token refresh was rejected — run 'fieldkit auth google'",
        ),
    ],
)
def test_doctor_google_refresh_cli_preserves_typed_payload_free_exit(
    tmp_path: Path,
    as_json: bool,
    failure: Exception,
    expected_exit: int,
    expected_message: str,
) -> None:
    token_file = tmp_path / "google-oauth-token.json"
    token_file.write_text('{"token": "fake"}', encoding="utf-8")
    mock_creds = MagicMock(expired=True, refresh_token="tok", valid=False)
    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=token_file),
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch("fieldkit.commands.doctor.google.refresh_google_credentials", side_effect=failure),
    ):
        result = CliRunner().invoke(doctor_google_cmd, ["--json"] if as_json else [])

    assert result.exit_code == expected_exit
    assert "fictional-private" not in result.output
    if as_json:
        assert json.loads(result.output)["message"] == expected_message
    else:
        assert expected_message in result.output


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


def test_check_google_retired_environment_aliases_do_not_configure_credentials(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GOOGLE_CLIENT_ID=test-id\nGOOGLE_CLIENT_SECRET=test-secret\n", encoding="utf-8")
    with (
        patch("fieldkit.commands.doctor.google.get_google_token_path", return_value=tmp_path / "absent.json"),
        patch("fieldkit.commands.doctor.google.get_fieldkit_home", return_value=tmp_path),
        patch.dict("os.environ", {}, clear=True),
    ):
        result = check_google()

    assert result.healthy is False
    assert result.configured is False
    assert result.exit_code == 2


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

        mock_sf.return_value = DoctorResult("sf", False, False, "session absent", failure_kind="auth")
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

        mock_sf.return_value = DoctorResult("sf", False, False, "session absent", failure_kind="auth")
        mock_gmail.return_value = DoctorResult("gmail", False, False, "optional", failure_kind="data")
        mock_google.return_value = DoctorResult("google", False, False, "optional", failure_kind="auth")
        mock_shadowbot.return_value = DoctorResult("shadowbot", False, False, "optional", failure_kind="auth")

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
    assert all(
        set(entry) == {"service", "healthy", "configured", "enabled", "configuration_state", "message"}
        for entry in payload
    )
    assert all(entry["enabled"] is True for entry in payload)


def test_doctor_bare_dispatches_to_subcommand(tmp_path: Path) -> None:
    """Bare `doctor sf` must run only the sf check, not all services."""
    cookie_file = tmp_path / "sf-cookies.json"
    with patch("fieldkit.commands.doctor.sf.get_cookie_file", return_value=cookie_file):
        result = CliRunner().invoke(cli, ["sf"])
    assert "sf:" in result.output
    assert "gmail:" not in result.output
