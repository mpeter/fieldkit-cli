"""Unit tests for fieldkit.contact.find_cmd — in-process coverage.

Covers _fmt_date(), _print_human(), _print_json(), and main() with mocked
contact_resolver.resolve and scan_pursuit_affiliations so no real DB access occurs.
"""

import json
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_RESOLVED_RESULT: dict[str, Any] = {
    "type": "resolved",
    "email": "alice@acme.example.com",
    "display_name": "Alice Smith",
    "domain": "acme.example.com",
    "account": "acme",
    "is_internal": False,
    "message_count": 42,
    "thread_count": 10,
    "initiated_count": 5,
    "meeting_count": 3,
    "first_seen": "2025-01-15T08:00:00",
    "last_seen": "2026-05-01T09:00:00",
    "slack_user_id": None,
    "slack_message_count": 0,
    "champion_signal": "strong",
    "decay_signal": "engaged",
    "recent_threads": [
        {
            "date_str": "Wed, 6 May 2026 09:12:50 -0400",
            "subject": "Pricing discussion",
            "snippet": "Let's align on Q2 pricing",
        }
    ],
    "recent_meetings": [
        {"start_time": "2026-04-20T14:00:00", "summary": "Discovery call"},
    ],
}

_NOT_FOUND_RESULT: dict[str, Any] = {
    "type": "not_found",
    "email": "nobody@nowhere.example.com",  # pii-guard: ignore
    "candidates": [],
}

_AMBIGUOUS_RESULT: dict[str, Any] = {
    "type": "ambiguous",
    "query": "John",
    "candidates": [
        {
            "email": "john.a@corp.example.com",  # pii-guard: ignore
            "display_name": "John A",
            "account": "corp",
            "message_count": 5,
        },  # pii-guard: ignore
        {
            "email": "john.b@corp.example.com",  # pii-guard: ignore
            "display_name": "John B",
            "account": "corp",
            "message_count": 2,
        },  # pii-guard: ignore
    ],
}

_AFFILIATIONS: list[dict[str, str | None]] = [
    {
        "pursuit_file": "acme/pursuits/platform-opp.md",
        "name": "Alice Smith",
        "title": "VP Engineering",
        "support": None,
        "meddpicc_role": "Champion",
        "notes": None,
    }
]


# ---------------------------------------------------------------------------
# _fmt_date
# ---------------------------------------------------------------------------


def test_fmt_date_none_returns_dash() -> None:
    from fieldkit.commands.contact.find_cmd import _fmt_date

    assert _fmt_date(None) == "—"


def test_fmt_date_iso_returns_first_10_chars() -> None:
    from fieldkit.commands.contact.find_cmd import _fmt_date

    assert _fmt_date("2026-05-01T09:00:00") == "2026-05-01"


def test_fmt_date_short_string_returns_full() -> None:
    from fieldkit.commands.contact.find_cmd import _fmt_date

    # Strings shorter than 10 chars are returned as-is ([:10] of a 4-char str = the str)
    assert _fmt_date("2026") == "2026"


def test_fmt_date_exact_10_chars() -> None:
    from fieldkit.commands.contact.find_cmd import _fmt_date

    assert _fmt_date("2026-05-01") == "2026-05-01"


# ---------------------------------------------------------------------------
# _print_human — not_found
# ---------------------------------------------------------------------------


def test_print_human_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_NOT_FOUND_RESULT, None)
    out = capsys.readouterr().out
    assert "No contact found" in out
    assert "nobody@nowhere.example.com" in out  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _print_human — ambiguous
# ---------------------------------------------------------------------------


def test_print_human_ambiguous_shows_candidates(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_AMBIGUOUS_RESULT, None)
    out = capsys.readouterr().out
    assert "Ambiguous" in out
    assert "john.a@corp.example.com" in out  # pii-guard: ignore
    assert "john.b@corp.example.com" in out  # pii-guard: ignore


def test_print_human_ambiguous_re_run_hint(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_AMBIGUOUS_RESULT, None)
    out = capsys.readouterr().out
    assert "Re-run" in out


# ---------------------------------------------------------------------------
# _print_human — resolved, no affiliations
# ---------------------------------------------------------------------------


def test_print_human_resolved_shows_identity(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "Alice Smith" in out
    assert "alice@acme.example.com" in out
    assert "acme.example.com" in out


def test_print_human_resolved_shows_communication(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "42" in out  # message_count
    assert "10" in out  # thread_count


def test_print_human_resolved_shows_signals(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "strong" in out
    assert "engaged" in out


def test_print_human_resolved_shows_recent_threads(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "Pricing discussion" in out


def test_print_human_resolved_shows_recent_meetings(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "Discovery call" in out


# ---------------------------------------------------------------------------
# _print_human — resolved with affiliations
# ---------------------------------------------------------------------------


def test_print_human_resolved_with_affiliations(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, _AFFILIATIONS)
    out = capsys.readouterr().out
    assert "PURSUIT AFFILIATIONS" in out
    assert "platform-opp.md" in out
    assert "Champion" in out


def test_print_human_resolved_empty_affiliations(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, [])
    out = capsys.readouterr().out
    assert "PURSUIT AFFILIATIONS" in out
    assert "None found" in out


def test_print_human_no_affiliations_section_when_none(capsys: pytest.CaptureFixture[str]) -> None:
    """When affiliations param is None (--affiliations not passed), no section header."""
    from fieldkit.commands.contact.find_cmd import _print_human

    _print_human(_RESOLVED_RESULT, None)
    out = capsys.readouterr().out
    assert "PURSUIT AFFILIATIONS" not in out


# ---------------------------------------------------------------------------
# _print_human — internal contact (Slack fields)
# ---------------------------------------------------------------------------


def test_print_human_internal_shows_slack_fields(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import _print_human

    internal_result = {**_RESOLVED_RESULT, "is_internal": True, "slack_user_id": "U12345", "slack_message_count": 17}
    _print_human(internal_result, None)
    out = capsys.readouterr().out
    assert "U12345" in out
    assert "17" in out


# ---------------------------------------------------------------------------
# _print_json (via main with --json)
# ---------------------------------------------------------------------------


def test_print_json_resolved_is_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with (
        patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT),
        patch("fieldkit.commands.contact.find_cmd.scan_pursuit_affiliations", return_value=[]),
    ):
        rc = cli.main(args=["alice@acme.example.com", "--json"], standalone_mode=False)

    assert rc is None
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["type"] == "resolved"
    assert parsed["email"] == "alice@acme.example.com"


def test_print_json_includes_affiliations_key_when_flag_set(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with (
        patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT),
        patch("fieldkit.commands.contact.find_cmd.scan_pursuit_affiliations", return_value=_AFFILIATIONS),
        patch(
            "fieldkit.commands.contact.find_cmd.get_accounts_root",
            return_value=__import__("pathlib").Path("/tmp/fake-accounts"),
        ),
    ):
        rc = cli.main(args=["alice@acme.example.com", "--affiliations", "--json"], standalone_mode=False)

    assert rc is None
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "affiliations" in parsed
    assert parsed["affiliations"][0]["meddpicc_role"] == "Champion"


def test_print_json_no_affiliations_key_without_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT):
        rc = cli.main(args=["alice@acme.example.com", "--json"], standalone_mode=False)

    assert rc is None
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "affiliations" not in parsed


# ---------------------------------------------------------------------------
# main() — resolved, human output
# ---------------------------------------------------------------------------


def test_main_resolved_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT):
        rc = cli.main(args=["alice@acme.example.com"], standalone_mode=False)

    assert rc is None


def test_main_resolved_calls_resolve_with_query() -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT) as mock_resolve:
        cli.main(args=["alice@acme.example.com"], standalone_mode=False)

    mock_resolve.assert_called_once_with("alice@acme.example.com", None)
    assert mock_resolve.call_count == 1


def test_main_not_found_exits_0() -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_NOT_FOUND_RESULT):
        rc = cli.main(args=["nobody@nowhere.example.com"], standalone_mode=False)  # pii-guard: ignore

    assert rc is None


# ---------------------------------------------------------------------------
# main() — --affiliations flag
# ---------------------------------------------------------------------------


def test_main_affiliations_flag_calls_scan(capsys: pytest.CaptureFixture[str]) -> None:
    from pathlib import Path

    from fieldkit.commands.contact.find_cmd import cli

    fake_accounts_root = Path("/fake/accounts")
    with (
        patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT),
        patch("fieldkit.commands.contact.find_cmd.scan_pursuit_affiliations", return_value=_AFFILIATIONS) as mock_scan,
        patch("fieldkit.commands.contact.find_cmd.get_accounts_root", return_value=fake_accounts_root),
    ):
        cli.main(args=["alice@acme.example.com", "--affiliations"], standalone_mode=False)

    # FIX 2: accounts_root is now passed explicitly via get_accounts_root()
    mock_scan.assert_called_once_with("alice@acme.example.com", accounts_root=fake_accounts_root)
    assert mock_scan.call_count == 1


def test_main_without_affiliations_flag_does_not_call_scan() -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with (
        patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_RESOLVED_RESULT),
        patch("fieldkit.commands.contact.find_cmd.scan_pursuit_affiliations") as mock_scan,
    ):
        cli.main(args=["alice@acme.example.com"], standalone_mode=False)

    mock_scan.assert_not_called()
    assert mock_scan.call_count == 0


# ---------------------------------------------------------------------------
# main() — --db flag passes path to resolve
# ---------------------------------------------------------------------------


def test_main_db_flag_passed_to_resolve(tmp_path: pytest.TempPathFactory) -> None:
    from pathlib import Path

    from fieldkit.commands.contact.find_cmd import cli

    db_path = str(tmp_path / "custom.db") if hasattr(tmp_path, "__truediv__") else "/tmp/custom.db"

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_NOT_FOUND_RESULT) as mock_resolve:
        cli.main(args=["alice@acme.example.com", "--db", db_path], standalone_mode=False)

    call_args = mock_resolve.call_args
    assert call_args[0][1] == Path(db_path)


# ---------------------------------------------------------------------------
# main() — ambiguous result
# ---------------------------------------------------------------------------


def test_main_ambiguous_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.contact.find_cmd import cli

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=_AMBIGUOUS_RESULT):
        rc = cli.main(args=["John"], standalone_mode=False)

    assert rc is None
    out = capsys.readouterr().out
    assert "Ambiguous" in out


# ---------------------------------------------------------------------------
# _print_human — None fields in resolved profile must not crash (historic regression)
# ---------------------------------------------------------------------------


def test_print_human_none_fields_no_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_human must not raise TypeError when resolved profile has None optional fields."""
    from fieldkit.commands.contact.find_cmd import _print_human

    profile_with_nones: dict[str, Any] = {
        "type": "resolved",
        "email": "user@example.com",  # pii-guard: ignore
        "display_name": None,
        "domain": None,
        "account": None,
        "is_internal": False,
        "message_count": None,
        "thread_count": None,
        "initiated_count": None,
        "meeting_count": None,
        "first_seen": None,
        "last_seen": None,
        "slack_user_id": None,
        "slack_message_count": None,
        "champion_signal": None,
        "decay_signal": None,
        "recent_threads": [],
        "recent_meetings": [],
    }
    # Must not raise
    _print_human(profile_with_nones, None)
    out = capsys.readouterr().out
    assert "user@example.com" in out  # pii-guard: ignore


def test_print_human_ambiguous_none_account_no_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_ambiguous must not raise TypeError when account=None (historic regression)."""
    from fieldkit.commands.contact.find_cmd import _print_human

    ambiguous = {
        "type": "ambiguous",
        "query": "john",
        "candidates": [
            {"email": "john@acme.example.com", "display_name": None, "account": None, "message_count": None},
            {"email": "john2@acme-corp.example.com", "display_name": "John B", "account": "acme", "message_count": 5},
        ],
    }
    _print_human(ambiguous, None)
    out = capsys.readouterr().out
    assert "john@acme.example.com" in out
    assert "john2@acme-corp.example.com" in out


def test_print_threads_none_snippet_no_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_threads must not crash when snippet=None (historic regression)."""
    from fieldkit.commands.contact.find_cmd import _print_threads

    threads = [{"date_str": "2026-01-15", "subject": "Q1 Review", "snippet": None}]
    _print_threads(threads)
    out = capsys.readouterr().out
    assert "Q1 Review" in out


# ---------------------------------------------------------------------------
# main() — null candidates in ambiguous result must not crash (historic regression)
# ---------------------------------------------------------------------------


def test_main_ambiguous_null_candidates_no_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI must not raise TypeError when resolver returns candidates=null (historic regression).

    The resolver can explicitly return {"candidates": null} for an ambiguous result.
    dict.get("candidates", []) returns None in that case (the default only applies
    when the key is absent), causing len(None) to raise TypeError.  The fix uses
    ``result.get("candidates") or []`` so an explicit null is treated as empty.
    """
    from fieldkit.commands.contact.find_cmd import cli

    null_candidates_result: dict[str, Any] = {
        "type": "ambiguous",
        "query": "ghost",
        "candidates": None,
    }

    with patch("fieldkit.commands.contact.find_cmd.resolve", return_value=null_candidates_result):
        rc = cli.main(args=["ghost"], standalone_mode=False)

    # Must exit cleanly — no TypeError raised
    assert rc is None
    out = capsys.readouterr().out
    assert "Ambiguous" in out
