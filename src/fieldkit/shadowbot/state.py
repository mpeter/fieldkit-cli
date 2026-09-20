"""Secure, session-scoped persistence for ShadowBot thread identifiers."""

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fieldkit.errors import FieldkitError
from fieldkit.shadowbot.auth import get_state_dir

_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    r"|^[a-zA-Z0-9_-]+$"
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STATE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_DEFAULT_STATE_FILENAME = "shadowbot-state.json"
_TEMP_CREATE_ATTEMPTS = 10


class ShadowbotStateError(FieldkitError):
    """Raised when secure ShadowBot state access fails."""


@dataclass(frozen=True)
class StateTarget:
    """Invocation-scoped state target pinned to an opened directory."""

    root_fd: int
    filename: str

    def close(self) -> None:
        try:
            os.close(self.root_fd)
        except OSError as exc:
            raise ShadowbotStateError("Failed to close ShadowBot state directory") from exc


@contextmanager
def state_target() -> Iterator[StateTarget]:
    """Own one selected target while preserving any active primary exception."""
    target = select_state_target()
    try:
        yield target
    except BaseException as primary:
        try:
            target.close()
        except ShadowbotStateError as close_error:
            primary.add_note(f"ShadowBot state directory close also failed: {close_error}")
        raise
    else:
        target.close()


def _selector_filename(state_dir: Path) -> str:
    if "SHADOWBOT_STATE_FILE" in os.environ:
        raw = os.environ["SHADOWBOT_STATE_FILE"]
        if not raw or raw.strip() != raw or not _STATE_FILENAME_RE.fullmatch(Path(raw).name):
            raise ShadowbotStateError("Invalid SHADOWBOT_STATE_FILE")
        path = Path(raw)
        if path.is_absolute():
            if path.parent != state_dir or path == state_dir:
                raise ShadowbotStateError("SHADOWBOT_STATE_FILE must be a direct child of the state directory")
        elif path.parent != Path() or path.name in {".", ".."}:
            raise ShadowbotStateError("SHADOWBOT_STATE_FILE must be one direct-child filename")
        return path.name

    for variable in ("SHADOWBOT_SESSION_ID", "CLAUDE_SESSION_ID"):
        if variable not in os.environ:
            continue
        session_id = os.environ[variable]
        if _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ShadowbotStateError(f"Invalid {variable}")
        return f"shadowbot-state-{session_id}.json"
    return _DEFAULT_STATE_FILENAME


def _add_cleanup_notes(primary: BaseException, errors: list[OSError]) -> None:
    for error in errors:
        primary.add_note(f"ShadowBot state cleanup also failed: {error}")


def select_state_target() -> StateTarget:
    """Select and securely open the invocation-scoped state target."""
    state_dir = get_state_dir()
    filename = _selector_filename(state_dir)
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ShadowbotStateError("Failed to open ShadowBot state directory") from exc

    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ShadowbotStateError("ShadowBot state root is not a directory")
        try:
            target_mode = os.stat(filename, dir_fd=root_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            target_mode = None
        if target_mode is not None and not stat.S_ISREG(target_mode):
            raise ShadowbotStateError("ShadowBot state target must be a regular file")
        return StateTarget(root_fd=root_fd, filename=filename)
    except BaseException as primary:
        try:
            os.close(root_fd)
        except OSError as cleanup_error:
            primary.add_note(f"ShadowBot state directory close also failed: {cleanup_error}")
        if isinstance(primary, ShadowbotStateError):
            raise
        raise ShadowbotStateError("Failed to validate ShadowBot state target") from primary


def load_thread_id(target: StateTarget) -> str | None:
    """Load a valid persisted thread ID, or None for missing/malformed state."""
    try:
        fd = os.open(
            target.filename,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=target.root_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShadowbotStateError("Failed to open ShadowBot state file") from exc

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ShadowbotStateError("ShadowBot state target must be a regular file")
        with os.fdopen(fd, encoding="utf-8") as state_file:
            fd = -1
            try:
                data: object = json.load(state_file)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
    except BaseException as primary:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as cleanup_error:
                primary.add_note(f"ShadowBot state file close also failed: {cleanup_error}")
        if isinstance(primary, ShadowbotStateError):
            raise
        if isinstance(primary, OSError):
            raise ShadowbotStateError("Failed to read ShadowBot state file") from primary
        raise

    if not isinstance(data, dict):
        return None
    tid = data.get("thread_id")
    return tid if isinstance(tid, str) and _THREAD_ID_RE.fullmatch(tid) else None


def _cleanup_temp(target: StateTarget, fd: int, temp_name: str | None) -> list[OSError]:
    errors: list[OSError] = []
    if fd >= 0:
        try:
            os.close(fd)
        except OSError as error:
            errors.append(error)
    if temp_name is not None:
        try:
            os.unlink(temp_name, dir_fd=target.root_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(error)
    return errors


def save_thread_id(target: StateTarget, tid: str) -> None:
    """Persist a thread ID using descriptor-relative private atomic replacement."""
    payload = json.dumps({"thread_id": tid}, indent=2).encode()
    temp_name: str | None = None
    fd = -1
    try:
        for _ in range(_TEMP_CREATE_ATTEMPTS):
            candidate = f".shadowbot-tmp-{secrets.token_hex(8)}"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=target.root_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if temp_name is None:
            raise ShadowbotStateError("Failed to allocate ShadowBot state temporary file")
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written == 0:
                raise OSError("short write while saving ShadowBot state")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_name, target.filename, src_dir_fd=target.root_fd, dst_dir_fd=target.root_fd)
        temp_name = None
    except BaseException as primary:
        _add_cleanup_notes(primary, _cleanup_temp(target, fd, temp_name))
        if isinstance(primary, ShadowbotStateError):
            raise
        if isinstance(primary, OSError):
            raise ShadowbotStateError("Failed to save ShadowBot state file") from primary
        raise
