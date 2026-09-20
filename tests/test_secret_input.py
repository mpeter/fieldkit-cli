"""Tests for secret-file inputs used by credential commands."""

from pathlib import Path

import pytest

from fieldkit.protected_input import SecretInputError, read_secret_file

pytestmark = pytest.mark.unit


def test_read_secret_file_accepts_an_owner_only_regular_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(" secret-value\n", encoding="utf-8")
    secret_file.chmod(0o600)

    result = read_secret_file(secret_file, label="test secret")

    assert result == "secret-value"


def test_read_secret_file_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("secret-value", encoding="utf-8")
    target.chmod(0o600)
    secret_file = tmp_path / "secret"
    secret_file.symlink_to(target)

    with pytest.raises(SecretInputError, match="regular file"):
        read_secret_file(secret_file, label="test secret")


def test_read_secret_file_rejects_group_or_world_readable_mode(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("secret-value", encoding="utf-8")
    secret_file.chmod(0o644)

    with pytest.raises(SecretInputError, match="chmod 600"):
        read_secret_file(secret_file, label="test secret")
