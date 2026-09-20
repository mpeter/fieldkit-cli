"""Tests for `fieldkit web serve` (src/fieldkit/commands/web/cli.py::serve)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.web.cli import cli, serve

pytestmark = pytest.mark.unit

runner = CliRunner()


def _make_token_file(tmp_path: Path, content: str, name: str = "web-token") -> Path:
    """Write a token file with the given raw content and return its path."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_serve_default_token_file_none_calls_run_server_with_default_host_port() -> None:
    """No --token-file → token=None, default host/port 127.0.0.1:6096, echo matches."""
    with patch("fieldkit.web.server.serve") as mock_run_server:
        result = runner.invoke(serve)
    assert result.exit_code == 0
    assert "fieldkit web → http://127.0.0.1:6096/" in result.output
    mock_run_server.assert_called_once_with(host="127.0.0.1", port=6096, token=None)


def test_serve_token_file_not_a_file_raises_and_does_not_start_server(tmp_path: Path) -> None:
    """--token-file pointing at a nonexistent path raises WebDataError, server not started."""
    missing = tmp_path / "does-not-exist"
    with patch("fieldkit.web.server.serve") as mock_run_server:
        result = runner.invoke(cli, ["serve", "--token-file", str(missing)])
    assert result.exit_code != 0
    assert "token file not found" in result.output
    assert "fieldkit web token" in result.output
    mock_run_server.assert_not_called()


def test_serve_token_file_is_a_directory_raises_and_does_not_start_server(tmp_path: Path) -> None:
    """--token-file pointing at a directory (not a file) also raises WebDataError."""
    directory = tmp_path / "a-directory"
    directory.mkdir()
    with patch("fieldkit.web.server.serve") as mock_run_server:
        result = runner.invoke(cli, ["serve", "--token-file", str(directory)])
    assert result.exit_code != 0
    assert "token file not found" in result.output
    assert "fieldkit web token" in result.output
    mock_run_server.assert_not_called()


def test_serve_token_file_empty_after_strip_raises_and_does_not_start_server(tmp_path: Path) -> None:
    """A real file containing only whitespace is treated as an empty token."""
    token_path = _make_token_file(tmp_path, "   \n\t  \n")
    with patch("fieldkit.web.server.serve") as mock_run_server:
        result = runner.invoke(cli, ["serve", "--token-file", str(token_path)])
    assert result.exit_code != 0
    assert "token file is empty" in result.output
    assert "regenerate" in result.output
    mock_run_server.assert_not_called()


def test_serve_token_file_with_real_token_is_stripped_and_passed_through(tmp_path: Path) -> None:
    """A real token file with incidental whitespace is stripped before being passed to run_server."""
    token_path = _make_token_file(tmp_path, "  \n my-test-token-123 \t\n")
    with patch("fieldkit.web.server.serve") as mock_run_server:
        result = runner.invoke(cli, ["serve", "--token-file", str(token_path)])
    assert result.exit_code == 0
    mock_run_server.assert_called_once_with(host="127.0.0.1", port=6096, token="my-test-token-123")


def test_serve_rejects_non_loopback_host_before_starting_server() -> None:
    """The built-in listener never offers plaintext network access."""
    result = runner.invoke(cli, ["serve", "--host", "0.0.0.0", "--port", "9999"])

    assert result.exit_code != 0
    assert "loopback" in result.output
