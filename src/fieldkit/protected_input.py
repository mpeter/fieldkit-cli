"""Safe file-based input for credentials that must not appear in process argv."""

import errno
import os
import stat
from pathlib import Path

from fieldkit.errors import FieldkitError


class SecretInputError(FieldkitError):
    """Raised when a secret-input file is unsafe or unusable."""


def read_secret_file(path: Path, *, label: str) -> str:
    """Read one non-empty owner-only regular secret file without following a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretInputError(
                f"{label.capitalize()} file must be a regular file, not a symlink or directory"
            ) from exc
        raise SecretInputError(f"Could not read {label} file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretInputError(f"{label.capitalize()} file must be a regular file, not a symlink or directory")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecretInputError(f"{label.capitalize()} file must be owner-only (chmod 600 {path})")
        secret_file = os.fdopen(descriptor, encoding="utf-8")
        descriptor = -1
        with secret_file:
            value = secret_file.read().strip()
    except OSError as exc:
        raise SecretInputError(f"Could not read {label} file: {path}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if not value:
        raise SecretInputError(f"{label.capitalize()} file must not be empty")
    return value
