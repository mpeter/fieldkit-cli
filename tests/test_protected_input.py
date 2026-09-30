"""Credential file input is bounded and never reflects private input."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.__main__ import main
from fieldkit.protected_input import SecretInputError, read_secret_file

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("payload", [b"x" * 8193, b"private-secret\xff"], ids=["oversized", "invalid-utf8"])
def test_invalid_secret_file_is_safe(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "private-input"
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(SecretInputError) as caught:
        read_secret_file(path, label="credential")

    assert str(path) not in str(caught.value)
    assert "private-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_secret_file_accepts_exact_limit(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "credential"
    path.write_bytes(b"x" * 8192)
    path.chmod(mode)

    result = read_secret_file(path, label="credential")

    assert result == "x" * 8192


def test_secret_file_rejects_fifo(tmp_path: Path) -> None:
    path = tmp_path / "credential"
    os.mkfifo(path, mode=0o600)

    with pytest.raises(SecretInputError, match="regular file"):
        read_secret_file(path, label="credential")


@pytest.mark.parametrize("command,option", [("sf", "--sid-file"), ("shadowbot", "--refresh-token-file")])
@pytest.mark.parametrize("kind", ["oversized", "utf8", "fifo", "symlink", "missing", "empty", "permissions"])
def test_cli_refuses_unsafe_credentials_before_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, option: str, kind: str
) -> None:
    path = tmp_path / "private-input"
    if kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing-target")
    elif kind != "missing":
        payloads = {"oversized": b"x" * 8193, "utf8": b"\xff", "empty": b"", "permissions": b"secret-sentinel"}
        path.write_bytes(payloads[kind])
        path.chmod(0o644 if kind == "permissions" else 0o600)

    with (
        patch("fieldkit.commands.auth.sf._authenticate_sid") as authenticate,
        patch("fieldkit.shadowbot.auth.inject_refresh_token") as inject,
    ):
        result = main(["auth", command, option, str(path)])

    assert result == 3
    authenticate.assert_not_called()
    inject.assert_not_called()
    output = capsys.readouterr()
    assert str(path) not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert "secret-sentinel" not in output.out + output.err
    expected = {
        "oversized": "8192 bytes",
        "utf8": "UTF-8",
        "fifo": "regular file",
        "symlink": "regular file",
        "missing": "Could not read",
        "empty": "must not be empty",
        "permissions": "owner-only",
    }
    assert expected[kind] in output.err
