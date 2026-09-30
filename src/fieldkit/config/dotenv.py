"""Safe dotenv loading and private, exact-value dotenv publication."""

import logging
import re
import stat
import tempfile
import warnings
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key
from dotenv.parser import parse_stream

from fieldkit.config._loader import ConfigError
from fieldkit.util.atomic import atomic_text_write
from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot

_DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_UNSUPPORTED_VALUE_CHARACTERS = ("\0", "\r")
_MAX_DOTENV_BYTES = 1024 * 1024


def validate_dotenv_values(values: Mapping[str, str]) -> None:
    """Reject values that cannot be represented exactly in an environment."""
    if any(not isinstance(key, str) or _DOTENV_KEY.fullmatch(key) is None for key in values):
        raise ConfigError("Credential environment keys are invalid")
    if any(not isinstance(value, str) for value in values.values()):
        raise ConfigError("Credential environment values are invalid")
    if any(character in value for value in values.values() for character in _UNSUPPORTED_VALUE_CHARACTERS):
        raise ConfigError("Credential environment values contain unsupported characters")


def _canonical_dotenv_destination(
    path: Path,
) -> tuple[Path, tuple[int, int], tuple[int, int, int, int] | None]:
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = parent.stat()
    except (OSError, RuntimeError):
        raise ConfigError("Credential environment directory is unavailable") from None
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ConfigError("Credential environment directory is unavailable")
    destination = parent / path.name
    try:
        destination_stat = destination.lstat()
    except FileNotFoundError:
        destination_identity = None
    except OSError:
        raise ConfigError("Credential environment destination could not be inspected") from None
    else:
        if not stat.S_ISREG(destination_stat.st_mode):
            raise ConfigError("Credential environment destination is not a regular file")
        destination_identity = (
            destination_stat.st_dev,
            destination_stat.st_ino,
            destination_stat.st_size,
            destination_stat.st_mtime_ns,
        )
    return destination, (parent_stat.st_dev, parent_stat.st_ino), destination_identity


def _read_existing_dotenv(
    destination: Path, expected_identity: tuple[int, int, int, int] | None
) -> TextSnapshot | None:
    if expected_identity is None:
        return None
    try:
        snapshot = read_text_snapshot(destination, max_bytes=_MAX_DOTENV_BYTES)
    except (FileNotFoundError, ValueError):
        raise ConfigError("Existing credential environment file is unsafe or too large") from None
    if snapshot.identity != expected_identity:
        raise ConfigError("Existing credential environment file changed during update")
    keys: set[str] = set()
    for binding in parse_stream(StringIO(snapshot.content)):
        if binding.error:
            raise ConfigError("Existing credential environment file is invalid")
        if binding.key is None:
            continue
        if binding.key in keys:
            raise ConfigError("Existing credential environment file is invalid")
        keys.add(binding.key)
    return snapshot


def _assert_dotenv_unchanged(destination: Path, original: TextSnapshot | None) -> None:
    try:
        current = read_text_snapshot(destination, max_bytes=_MAX_DOTENV_BYTES)
    except FileNotFoundError:
        if original is None:
            return
        raise ConfigError("Existing credential environment file changed during update") from None
    except ValueError:
        raise ConfigError("Existing credential environment file changed during update") from None
    if original is None or current.identity != original.identity or current.content != original.content:
        raise ConfigError("Existing credential environment file changed during update")


def write_dotenv_file(path: Path, values: Mapping[str, str]) -> None:
    """Atomically publish deterministic private dotenv values without interpolation.

    Values may contain Unicode, quotes, backslashes, dollar signs, and LF
    newlines. NUL cannot enter an operating-system environment, and python-dotenv
    normalizes carriage returns, so both are rejected before filesystem access.
    """
    validate_dotenv_values(values)
    destination, parent_identity, destination_identity = _canonical_dotenv_destination(path)
    original = _read_existing_dotenv(destination, destination_identity)
    try:
        with tempfile.TemporaryDirectory(
            dir=destination.parent,
            prefix=f".{destination.name}.render.",
        ) as render_directory:
            staging_path = Path(render_directory) / "environment"
            staging_path.touch(mode=0o600)
            staging_path.chmod(0o600)
            staging_path.write_text(original.content if original is not None else "", encoding="utf-8")
            for key in sorted(values):
                result, written_key, written_value = set_key(
                    staging_path,
                    key,
                    values[key],
                    quote_mode="always",
                    export=False,
                    encoding="utf-8",
                    follow_symlinks=False,
                )
                if result is not True or written_key != key or written_value != values[key]:
                    raise OSError("dotenv serializer did not confirm the requested write")
            staging_stat = staging_path.stat()
            if not stat.S_ISREG(staging_stat.st_mode) or stat.S_IMODE(staging_stat.st_mode) != 0o600:
                raise OSError("dotenv serializer changed private file properties")
            rendered = staging_path.read_text(encoding="utf-8")
        destination, current_parent_identity, _ = _canonical_dotenv_destination(destination)
        if current_parent_identity != parent_identity:
            raise OSError("credential environment directory changed")
        _assert_dotenv_unchanged(destination, original)
        atomic_text_write(destination, rendered, mode=0o600)
    except ConfigError:
        raise
    except (OSError, ValueError):
        raise ConfigError("Credential environment file could not be written safely") from None


def load_dotenv_safe(**kwargs: Any) -> bool:
    """Call load_dotenv() suppressing UserWarnings and logger warnings from python-dotenv.

    fieldkit-data/.env and ~/.env use shell syntax (export KEY=val, if-blocks)
    that python-dotenv cannot parse.  It emits both a Python UserWarning *and*
    a logger.warning() via the "dotenv.main" logger.  This wrapper silences both
    so callers get clean stderr output without changing parse behaviour.

    Accepts and forwards all keyword arguments supported by load_dotenv().
    Interpolation defaults to false so credentials containing ``${NAME}`` load
    literally; callers may opt in explicitly. Existing environment values keep
    precedence unless the caller requests ``override=True``. Returns the same
    bool as load_dotenv() (true when a dotenv file was found).
    """
    kwargs.setdefault("interpolate", False)
    dotenv_logger = logging.getLogger("dotenv.main")
    original_level = dotenv_logger.level
    dotenv_logger.setLevel(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module=r"dotenv")
            return load_dotenv(**kwargs)
    finally:
        dotenv_logger.setLevel(original_level)
