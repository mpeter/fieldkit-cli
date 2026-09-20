"""Tests for fieldkit.meeting.docs_domain and fieldkit.commands.meeting.* CLI adapters."""

import subprocess
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.meeting.cli import cli
from fieldkit.meeting.docs_domain import (
    _account_name,
    _doc_url,
    _mcpjungle,
    _read_frontmatter,
)
from fieldkit.pursuit import write_frontmatter_raw

pytestmark = pytest.mark.unit

_DOMAIN = "fieldkit.meeting.docs_domain"


# ---------------------------------------------------------------------------
# historic regression: _mcpjungle must prefer stdout over stderr
# ---------------------------------------------------------------------------


def _mcpjungle_bug024_make_result(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def test_mcpjungle_bug024_returns_stdout_when_both_present() -> None:
    """historic regression: stdout is returned even when stderr also has content."""
    json_payload = '{"status": "ok"}'
    status_text = "Server started on port 8080"
    result = _mcpjungle_bug024_make_result(stdout=json_payload, stderr=status_text)

    with patch(f"{_DOMAIN}.subprocess.run", return_value=result):
        output = _mcpjungle("some-tool", {})

    assert output == json_payload


def test_mcpjungle_bug024_returns_stderr_when_stdout_empty() -> None:
    """Falls back to stderr when stdout is empty (pre-existing behaviour preserved)."""
    result = _mcpjungle_bug024_make_result(stdout="", stderr="fallback message")

    with patch(f"{_DOMAIN}.subprocess.run", return_value=result):
        output = _mcpjungle("some-tool", {})

    assert output == "fallback message"


def test_mcpjungle_bug024_raises_on_nonzero_returncode() -> None:
    """RuntimeError is raised when mcpjungle exits non-zero."""
    result = _mcpjungle_bug024_make_result(stdout="", stderr="command not found", returncode=1)

    with (
        patch(f"{_DOMAIN}.subprocess.run", return_value=result),
        pytest.raises(RuntimeError, match="mcpjungle some-tool failed"),
    ):
        _mcpjungle("some-tool", {})


# ---------------------------------------------------------------------------
# Helper: _doc_url
# ---------------------------------------------------------------------------


def test_doc_url() -> None:
    assert _doc_url("abc123") == "https://docs.google.com/document/d/abc123/edit"


# ---------------------------------------------------------------------------
# Helper: _read_frontmatter / _write_frontmatter round-trip
# ---------------------------------------------------------------------------


SAMPLE_MD = dedent("""\
    ---
    title: Test Pursuit
    stage: discover
    gdoc_workbook: ""
    ---

    # Test Pursuit

    Some body content here.
""")


def test_read_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    fm, body = _read_frontmatter(p)
    assert fm["title"] == "Test Pursuit"
    assert fm["stage"] == "discover"
    assert "# Test Pursuit" in body


def test_read_frontmatter_missing_delimiters(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("No frontmatter here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No YAML frontmatter"):
        _read_frontmatter(p)


def test_write_frontmatter_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    fm, body = _read_frontmatter(p)
    fm["gdoc_workbook"] = "new_doc_id_abc"
    write_frontmatter_raw(p, fm, body)

    fm2, body2 = _read_frontmatter(p)
    assert fm2["gdoc_workbook"] == "new_doc_id_abc"
    assert "# Test Pursuit" in body2


# ---------------------------------------------------------------------------
# CLI: command registration and --help
# ---------------------------------------------------------------------------


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "link" in result.output
    assert "open" in result.output
    assert "note" in result.output
    assert "list" in result.output


def test_link_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["link", "--help"])
    assert result.exit_code == 0
    assert "PURSUIT_FILE" in result.output
    assert "--open" in result.output


def test_open_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["open", "--help"])
    assert result.exit_code == 0
    assert "PURSUIT_FILE" in result.output


def test_note_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["note", "--help"])
    assert result.exit_code == 0
    assert "PURSUIT_FILE" in result.output
    assert "--open" in result.output


def test_list_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI: open exits EXIT_DATA when no workbook linked
# ---------------------------------------------------------------------------


def test_open_no_doc_linked(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["open", str(p)])
    assert result.exit_code == 3  # EXIT_DATA


# ---------------------------------------------------------------------------
# CLI: open succeeds when workbook is linked (mocked webbrowser)
# ---------------------------------------------------------------------------


def test_open_with_linked_doc(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    md = SAMPLE_MD.replace('gdoc_workbook: ""', "gdoc_workbook: doc123ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab")
    p.write_text(md, encoding="utf-8")
    runner = CliRunner()
    with patch("fieldkit.commands.meeting.open_cmd.webbrowser.open") as mock_open:
        result = runner.invoke(cli, ["open", str(p)])
    assert result.exit_code == 0
    mock_open.assert_called_once_with(
        "https://docs.google.com/document/d/doc123ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab/edit"
    )


# ---------------------------------------------------------------------------
# historic regression: note tab create call must NOT include 'index' key
# ---------------------------------------------------------------------------


def _bug272_tab_index_make_pursuit(tmp_path: Path) -> Path:
    p = tmp_path / "pursuit.md"
    md = SAMPLE_MD.replace('gdoc_workbook: ""', "gdoc_workbook: docABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd")
    p.write_text(md, encoding="utf-8")
    return p


def test_bug272_tab_index_create_call_has_no_index_key(tmp_path: Path) -> None:
    """The dict passed to _mcpjungle for action=create must not contain 'index'."""
    p = _bug272_tab_index_make_pursuit(tmp_path)
    captured_calls: list[dict[str, object]] = []

    def fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
        captured_calls.append({"tool": tool, "input": input_data})
        if input_data.get("action") == "create":
            return '{"result": {"tab_id": "t.1"}}'
        return ""

    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys.stdin.isatty", return_value=False),
    ):
        result = runner.invoke(cli, ["note", str(p), "--content", "notes here"])

    assert result.exit_code == 0, result.output
    create_calls = [c for c in captured_calls if c["input"].get("action") == "create"]  # type: ignore[union-attr]
    assert len(create_calls) == 1, "Expected exactly one create call"
    assert "index" not in create_calls[0]["input"], (
        "The 'index' key must be absent from the create payload (historic regression)"
    )


def test_bug272_tab_index_create_call_has_required_fields(tmp_path: Path) -> None:
    """The create payload must include document_id, action, title, user_google_email."""
    p = _bug272_tab_index_make_pursuit(tmp_path)
    captured: list[dict[str, object]] = []

    def fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
        captured.append(input_data)
        if input_data.get("action") == "create":
            return '{"result": {"tab_id": "t.1"}}'
        return ""

    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys.stdin.isatty", return_value=False),
    ):
        runner.invoke(cli, ["note", str(p), "--content", "notes"])

    create_payload = next(c for c in captured if c.get("action") == "create")
    assert create_payload["document_id"] == "docABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
    assert create_payload["action"] == "create"
    assert "title" in create_payload


# ---------------------------------------------------------------------------
# historic regression: link and open handle missing frontmatter without traceback
# ---------------------------------------------------------------------------


def _bug273_frontmatter_guard_no_fm_file(tmp_path: Path) -> Path:
    p = tmp_path / "no_fm.md"
    p.write_text("Just plain text, no YAML delimiters.\n", encoding="utf-8")
    return p


def test_bug273_frontmatter_guard_link_exits_data_on_missing_frontmatter(tmp_path: Path) -> None:
    p = _bug273_frontmatter_guard_no_fm_file(tmp_path)
    # capture='sys' separates stdout/stderr in Click 8.4+
    runner = CliRunner(capture="sys")
    result = runner.invoke(cli, ["link", str(p)])
    assert result.exit_code == 3, f"Expected EXIT_DATA(3), got {result.exit_code}"
    # Error message must name the file and must not be a raw Python traceback
    combined = result.stdout + result.stderr
    assert "no_fm.md" in combined
    assert "Traceback" not in combined


def test_bug273_frontmatter_guard_open_exits_data_on_missing_frontmatter(tmp_path: Path) -> None:
    p = _bug273_frontmatter_guard_no_fm_file(tmp_path)
    runner = CliRunner(capture="sys")
    result = runner.invoke(cli, ["open", str(p)])
    assert result.exit_code == 3, f"Expected EXIT_DATA(3), got {result.exit_code}"
    combined = result.stdout + result.stderr
    assert "no_fm.md" in combined
    assert "Traceback" not in combined


def test_bug273_frontmatter_guard_link_error_message_mentions_file(tmp_path: Path) -> None:
    p = _bug273_frontmatter_guard_no_fm_file(tmp_path)
    runner = CliRunner(capture="sys")
    result = runner.invoke(cli, ["link", str(p)])
    combined = result.stdout + result.stderr
    assert "No YAML frontmatter" in combined


def test_bug273_frontmatter_guard_open_error_message_mentions_file(tmp_path: Path) -> None:
    p = _bug273_frontmatter_guard_no_fm_file(tmp_path)
    runner = CliRunner(capture="sys")
    result = runner.invoke(cli, ["open", str(p)])
    combined = result.stdout + result.stderr
    assert "No YAML frontmatter" in combined


# ---------------------------------------------------------------------------
# historic regression: note suppresses prompt when stdin is not a tty
# ---------------------------------------------------------------------------


def _bug274_stdin_prompt_linked_pursuit(tmp_path: Path) -> Path:
    p = tmp_path / "pursuit.md"
    md = SAMPLE_MD.replace('gdoc_workbook: ""', "gdoc_workbook: docXYZ0123456789abcdefghijklmnopqrstuvwxyzA")
    p.write_text(md, encoding="utf-8")
    return p


def _bug274_stdin_prompt_make_sys_mock(*, isatty: bool) -> MagicMock:
    """Return a mock sys module with stdin.isatty() returning the given value.

    We patch the entire `sys` module reference in fieldkit.commands.meeting.note_cmd
    because CliRunner replaces sys.stdin with its own BytesIO object, making
    attribute patches on the real sys.stdin ineffective.
    """
    import sys as real_sys

    mock_sys = MagicMock(wraps=real_sys)
    mock_sys.stdin = MagicMock()
    mock_sys.stdin.isatty.return_value = isatty
    # Preserve sys.exit so cli_main() can still call it
    mock_sys.exit = real_sys.exit
    return mock_sys


def test_bug274_stdin_prompt_no_prompt_when_stdin_is_not_tty(tmp_path: Path) -> None:
    """When isatty() is False, the prompt string must not appear in output."""
    p = _bug274_stdin_prompt_linked_pursuit(tmp_path)

    def fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
        if input_data.get("action") == "create":
            return '{"result": {"tab_id": "t.1"}}'
        return ""

    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys", _bug274_stdin_prompt_make_sys_mock(isatty=False)),
    ):
        result = runner.invoke(cli, ["note", str(p), "--content", "piped content"])

    assert result.exit_code == 0, result.output
    assert "Meeting note content" not in result.output


def test_bug274_stdin_prompt_no_input_call_when_stdin_is_not_tty(tmp_path: Path) -> None:
    """When isatty() is False, input() must never be called."""
    p = _bug274_stdin_prompt_linked_pursuit(tmp_path)

    def fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
        if input_data.get("action") == "create":
            return '{"result": {"tab_id": "t.1"}}'
        return ""

    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys", _bug274_stdin_prompt_make_sys_mock(isatty=False)),
        patch("builtins.input") as mock_input,
    ):
        runner.invoke(cli, ["note", str(p), "--content", "piped"])

    mock_input.assert_not_called()


def test_bug274_stdin_prompt_prompt_shown_when_stdin_is_tty(tmp_path: Path) -> None:
    """When isatty() is True and no --content, the prompt must appear."""
    p = _bug274_stdin_prompt_linked_pursuit(tmp_path)

    def fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
        if input_data.get("action") == "create":
            return '{"result": {"tab_id": "t.1"}}'
        return ""

    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys", _bug274_stdin_prompt_make_sys_mock(isatty=True)),
        # Simulate Ctrl+D immediately so input() raises EOFError
        patch("builtins.input", side_effect=EOFError),
    ):
        result = runner.invoke(cli, ["note", str(p)])

    assert "Meeting note content" in result.output


# ---------------------------------------------------------------------------
# historic regression: _account_name fallback returns "" with warning
# ---------------------------------------------------------------------------


def test_bug275_account_name_fallback_standard_path_returns_slug_uppercased(tmp_path: Path) -> None:
    """Standard accounts/<slug>/pursuits/<file> path returns slug.upper()."""
    p = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    assert _account_name(p) == "ACME-CORP"


def test_bug275_account_name_fallback_nonstandard_path_returns_empty_string(tmp_path: Path) -> None:
    """Path without 'accounts' segment returns empty string (historic regression)."""
    p = tmp_path / "random" / "dir" / "deal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    result = _account_name(p)
    assert result == "", f"Expected '', got {result!r}"


def test_bug275_account_name_fallback_nonstandard_path_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning is logged when the path does not match the expected structure."""
    import logging

    p = tmp_path / "random" / "dir" / "deal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    with caplog.at_level(logging.WARNING, logger=_DOMAIN):
        _account_name(p)
    assert any("cannot infer account name" in r.message for r in caplog.records)


def test_bug275_account_name_fallback_nonstandard_path_warning_names_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning message includes the file path."""
    import logging

    p = tmp_path / "somewhere" / "deal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    with caplog.at_level(logging.WARNING, logger=_DOMAIN):
        _account_name(p)
    assert any(str(p) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# implementation change: meeting link --open flag
# ---------------------------------------------------------------------------


def _enh276_link_open_make_pursuit(tmp_path: Path) -> Path:
    p = tmp_path / "pursuit.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    return p


def _enh276_link_open_mock_drive_create() -> MagicMock:
    """Return a mock Drive service whose files().create().execute() returns a doc ID."""
    drive_mock = MagicMock()
    drive_mock.files.return_value.create.return_value.execute.return_value = {
        "id": "newdocid0123456789abcdefghijklmnopqrstuvwxy"
    }
    return drive_mock


def test_enh276_link_open_open_flag_triggers_webbrowser(tmp_path: Path) -> None:
    p = _enh276_link_open_make_pursuit(tmp_path)
    drive_mock = _enh276_link_open_mock_drive_create()

    # get_drive_service is a lazy import inside link(); patch at the source module
    with (
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch(f"{_DOMAIN}._set_pageless"),
        patch(f"{_DOMAIN}._mcpjungle", return_value=""),
        patch("fieldkit.ingest.docs.get_drive_service", return_value=drive_mock),
        patch("fieldkit.commands.meeting.link_cmd.webbrowser.open") as mock_wb,
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["link", "--open", str(p)])

    assert result.exit_code == 0, result.output
    mock_wb.assert_called_once_with(
        "https://docs.google.com/document/d/newdocid0123456789abcdefghijklmnopqrstuvwxy/edit"
    )


def test_enh276_link_open_no_open_flag_does_not_trigger_webbrowser(tmp_path: Path) -> None:
    p = _enh276_link_open_make_pursuit(tmp_path)
    drive_mock = _enh276_link_open_mock_drive_create()

    with (
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch(f"{_DOMAIN}._set_pageless"),
        patch(f"{_DOMAIN}._mcpjungle", return_value=""),
        patch("fieldkit.ingest.docs.get_drive_service", return_value=drive_mock),
        patch("fieldkit.commands.meeting.link_cmd.webbrowser.open") as mock_wb,
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["link", str(p)])

    assert result.exit_code == 0, result.output
    mock_wb.assert_not_called()


def test_enh276_link_open_url_printed_regardless_of_open_flag(tmp_path: Path) -> None:
    p = _enh276_link_open_make_pursuit(tmp_path)
    drive_mock = _enh276_link_open_mock_drive_create()

    with (
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch(f"{_DOMAIN}._set_pageless"),
        patch(f"{_DOMAIN}._mcpjungle", return_value=""),
        patch("fieldkit.ingest.docs.get_drive_service", return_value=drive_mock),
        patch("fieldkit.commands.meeting.link_cmd.webbrowser.open"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["link", str(p)])

    assert "newdocid0123456789abcdefghijklmnopqrstuvwxy" in result.output


# ---------------------------------------------------------------------------
# implementation change: meeting note --open flag
# ---------------------------------------------------------------------------


def _enh277_meeting_note_open_linked_pursuit(tmp_path: Path) -> Path:
    p = tmp_path / "pursuit.md"
    md = SAMPLE_MD.replace('gdoc_workbook: ""', "gdoc_workbook: docMNOPQRSTUVWXYZ0123456789abcdefghijklmnop")
    p.write_text(md, encoding="utf-8")
    return p


def _enh277_meeting_note_open_fake_mcpjungle(tool: str, input_data: dict[str, object]) -> str:
    if input_data.get("action") == "create":
        return '{"result": {"tab_id": "t.2"}}'
    return ""


def test_enh277_meeting_note_open_open_flag_triggers_webbrowser(tmp_path: Path) -> None:
    p = _enh277_meeting_note_open_linked_pursuit(tmp_path)
    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=_enh277_meeting_note_open_fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys.stdin.isatty", return_value=False),
        patch("fieldkit.commands.meeting.note_cmd.webbrowser.open") as mock_wb,
    ):
        result = runner.invoke(cli, ["note", "--open", str(p), "--content", "notes"])

    assert result.exit_code == 0, result.output
    mock_wb.assert_called_once_with(
        "https://docs.google.com/document/d/docMNOPQRSTUVWXYZ0123456789abcdefghijklmnop/edit"
    )


def test_enh277_meeting_note_open_no_open_flag_does_not_trigger_webbrowser(tmp_path: Path) -> None:
    p = _enh277_meeting_note_open_linked_pursuit(tmp_path)
    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=_enh277_meeting_note_open_fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys.stdin.isatty", return_value=False),
        patch("fieldkit.commands.meeting.note_cmd.webbrowser.open") as mock_wb,
    ):
        result = runner.invoke(cli, ["note", str(p), "--content", "notes"])

    assert result.exit_code == 0, result.output
    mock_wb.assert_not_called()


def test_enh277_meeting_note_open_confirmation_message_printed_regardless_of_open_flag(tmp_path: Path) -> None:
    p = _enh277_meeting_note_open_linked_pursuit(tmp_path)
    runner = CliRunner()
    with (
        patch(f"{_DOMAIN}._mcpjungle", side_effect=_enh277_meeting_note_open_fake_mcpjungle),
        patch(f"{_DOMAIN}._user_google_email", return_value="user@example.com"),  # pii-guard: ignore
        patch("fieldkit.commands.meeting.note_cmd.sys.stdin.isatty", return_value=False),
        patch("fieldkit.commands.meeting.note_cmd.webbrowser.open"),
    ):
        result = runner.invoke(cli, ["note", str(p), "--content", "notes"])

    assert "Added tab" in result.output
    assert "docMNOPQRSTUVWXYZ0123456789abcdefghijklmnop" in result.output


# ---------------------------------------------------------------------------
# implementation change: meeting list subcommand
# ---------------------------------------------------------------------------


_ENH278_DOCS_LIST__LINKED_MD = dedent("""\
    ---
    title: Linked Pursuit
    gdoc_workbook: workbook123ABCDEFGHIJKLMNOPQRSTUVWXYZ01234
    ---

    # Linked Pursuit
""")

_ENH278_DOCS_LIST__UNLINKED_MD = dedent("""\
    ---
    title: Unlinked Pursuit
    gdoc_workbook: ""
    ---

    # Unlinked Pursuit
""")

_ENH278_DOCS_LIST__NO_FM_MD = "Just plain text, no frontmatter.\n"


def _enh278_docs_list_setup_data_root(tmp_path: Path) -> Path:
    """Create a minimal data root with accounts/*/pursuits/ structure."""
    data_root = tmp_path / "fieldkit-data"
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    return data_root


def test_enh278_docs_list_linked_pursuit_is_printed(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    p = data_root / "accounts" / "acme-corp" / "pursuits" / "deal-a.md"
    p.write_text(_ENH278_DOCS_LIST__LINKED_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert "deal-a.md" in result.output
    assert "workbook123ABCDEFGHIJKLMNOPQRSTUVWXYZ01234" in result.output
    assert "docs.google.com" in result.output


def test_enh278_docs_list_unlinked_pursuit_is_not_printed(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    p = data_root / "accounts" / "acme-corp" / "pursuits" / "deal-b.md"
    p.write_text(_ENH278_DOCS_LIST__UNLINKED_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "deal-b.md" not in result.output


def test_enh278_docs_list_no_frontmatter_file_does_not_crash(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    p = data_root / "accounts" / "acme-corp" / "pursuits" / "broken.md"
    p.write_text(_ENH278_DOCS_LIST__NO_FM_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "broken.md" not in result.output


def test_enh278_docs_list_empty_data_root_exits_zero_with_no_output(tmp_path: Path) -> None:
    data_root = tmp_path / "empty-data"
    data_root.mkdir()

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_enh278_docs_list_output_sorted_alphabetically(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    for name in ("zzz-deal.md", "aaa-deal.md", "mmm-deal.md"):
        (pursuits_dir / name).write_text(_ENH278_DOCS_LIST__LINKED_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    names_in_output = [ln.split()[0] for ln in lines]
    assert names_in_output == sorted(names_in_output), "Output must be sorted alphabetically"


def test_enh278_docs_list_output_format_relative_path_and_url(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    p = data_root / "accounts" / "acme-corp" / "pursuits" / "deal-c.md"
    p.write_text(_ENH278_DOCS_LIST__LINKED_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    # Line must contain relative path (not absolute) and the doc URL
    line = next(ln for ln in result.output.splitlines() if "deal-c.md" in ln)
    assert str(data_root) not in line, "Path must be relative, not absolute"
    assert "https://docs.google.com/document/d/workbook123ABCDEFGHIJKLMNOPQRSTUVWXYZ01234/edit" in line


def test_enh278_docs_list_mixed_pursuits_only_linked_printed(tmp_path: Path) -> None:
    data_root = _enh278_docs_list_setup_data_root(tmp_path)
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    (pursuits_dir / "linked.md").write_text(_ENH278_DOCS_LIST__LINKED_MD, encoding="utf-8")
    (pursuits_dir / "unlinked.md").write_text(_ENH278_DOCS_LIST__UNLINKED_MD, encoding="utf-8")
    (pursuits_dir / "broken.md").write_text(_ENH278_DOCS_LIST__NO_FM_MD, encoding="utf-8")

    runner = CliRunner()
    with patch("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", return_value=data_root):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "linked.md" in result.output
    assert "unlinked.md" not in result.output
    assert "broken.md" not in result.output
