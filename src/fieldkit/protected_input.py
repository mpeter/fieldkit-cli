"""Safe file-based input for credentials that must not appear in process argv."""

import errno
import os
import stat
from pathlib import Path

from fieldkit.errors import FieldkitError

MAX_SECRET_FILE_BYTES = 8192


class SecretInputError(FieldkitError):
    """Raised when a secret-input file is unsafe or unusable."""


def read_secret_file(path: Path, *, label: str) -> str:
    """Read one non-empty owner-only regular secret file without following a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretInputError(
                f"{label.capitalize()} file must be a regular file, not a symlink or directory"
            ) from None
        raise SecretInputError(f"Could not read {label} file") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretInputError(f"{label.capitalize()} file must be a regular file, not a symlink or directory")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecretInputError(f"{label.capitalize()} file must be owner-only (chmod 600)")
        if metadata.st_size > MAX_SECRET_FILE_BYTES:
            raise SecretInputError(f"{label.capitalize()} file must not exceed {MAX_SECRET_FILE_BYTES} bytes") from None
        secret_file = os.fdopen(descriptor, "rb")
        descriptor = -1
        with secret_file:
            contents = secret_file.read(MAX_SECRET_FILE_BYTES + 1)
        if len(contents) > MAX_SECRET_FILE_BYTES:
            raise SecretInputError(f"{label.capitalize()} file must not exceed {MAX_SECRET_FILE_BYTES} bytes") from None
        value = contents.decode("utf-8").strip()
    except UnicodeError:
        raise SecretInputError(f"{label.capitalize()} file must contain UTF-8 text") from None
    except OSError:
        raise SecretInputError(f"Could not read {label} file") from None
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if not value:
        raise SecretInputError(f"{label.capitalize()} file must not be empty")
    return value
