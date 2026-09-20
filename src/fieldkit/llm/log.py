"""lib/llm_log.py — SQLite audit log for every LiteLLM call.

Registers success and failure callbacks with LiteLLM on import so that every
call made via lib.llm.synthesize() (or any direct litellm.completion() call)
is persisted to the configured fieldkit data root without touching calling code.

Schema
------
llm_calls
  id           TEXT PRIMARY KEY   — litellm call id (UUID)
  ts           TEXT NOT NULL      — ISO-8601 UTC timestamp of the call
  skill        TEXT               — value of LLM_SKILL context var
  account      TEXT               — value of LLM_ACCOUNT context var
  model        TEXT               — resolved model identifier
  input_tokens INTEGER            — prompt token count
  output_tokens INTEGER           — completion token count
  latency_ms   INTEGER            — wall-clock latency in milliseconds
  cost_usd     REAL               — estimated cost (from litellm pricing tables)
  error        TEXT               — exception message if call failed, else NULL
  prompt_hash  TEXT               — SHA-256 of the raw prompt (text NOT stored)

Context variables
-----------------
Callers set skill/account tags before making LLM calls:

    from fieldkit.llm_log import set_skill_context
    set_skill_context("morning-brief", "GlobalPay")

Or use the context vars directly:

    from fieldkit.llm_log import LLM_SKILL, LLM_ACCOUNT
    token = LLM_SKILL.set("meeting-prep")

Environment
-----------
FIELDKIT_LLM_LOG   — override the default DB path (<fieldkit_data>/llm-calls.db)
NO_LLM             — when set, litellm is not imported; callbacks are not registered
                     but init_db(), context vars, and set_skill_context still work.
"""

import hashlib
import logging
import os
import sqlite3
import threading as _threading
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import (
    ConfigError,
    get_configured_fieldkit_data,
    get_fieldkit_data,
    get_fieldkit_home,
    llm_disabled,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

_LEGACY_DB = Path.home() / ".local" / "share" / "fieldkit" / "llm-calls.db"
_OLD_DB = _LEGACY_DB.parent / "llm_calls.db"


def _resolve_log_override(raw: str | None) -> Path:
    """Resolve and validate an optional ``FIELDKIT_LLM_LOG`` value."""
    if raw is None:
        return get_fieldkit_data() / "llm-calls.db"

    if not raw.strip():
        raise ConfigError("FIELDKIT_LLM_LOG must not be empty or whitespace")
    if raw != raw.strip():
        raise ConfigError("FIELDKIT_LLM_LOG must not have leading or trailing whitespace")

    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(f"FIELDKIT_LLM_LOG must be an absolute path, got: {raw!r}")
        resolved = candidate.resolve()
    except ConfigError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"Could not resolve FIELDKIT_LLM_LOG path {raw!r}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError("FIELDKIT_LLM_LOG contains invalid path characters") from exc

    if not any(resolved == root or resolved.is_relative_to(root) for root in _approved_log_roots()):
        raise ConfigError(f"FIELDKIT_LLM_LOG path {resolved!r} is not under an approved fieldkit root")
    return resolved


def _approved_log_roots() -> set[Path]:
    """Return stable roots plus any available configured workspace roots."""
    roots = {
        (Path.home() / ".config" / "fieldkit").resolve(),
        (Path.home() / ".local" / "share" / "fieldkit").resolve(),
    }
    for root_resolver in (get_fieldkit_home, get_fieldkit_data, get_configured_fieldkit_data):
        try:
            roots.add(root_resolver().resolve())
        except (ConfigError, OSError, RuntimeError, ValueError):
            continue
    return roots


def get_db_path() -> Path:
    """Return the LLM log DB path, resolving overrides at call time.

    This public resolver is shared with :mod:`fieldkit.driver.spend` so the
    ``FIELDKIT_LLM_LOG`` rule has one home (implementation change).

    Explicit overrides must resolve beneath a P36-approved fieldkit storage
    root. The unset default follows the authoritative runtime-data root.

    Raises:
        ConfigError: If an explicit override is empty, relative, cannot be
            resolved, or escapes every approved root.
    """
    return _resolve_log_override(os.environ.get("FIELDKIT_LLM_LOG"))


def get_read_db_path() -> Path:
    """Return the configured database, falling back to legacy history."""
    path = get_db_path()
    if os.environ.get("FIELDKIT_LLM_LOG") is None and not path.exists() and _LEGACY_DB.exists():
        return _LEGACY_DB
    return path


def get_legacy_db_path() -> Path:
    """Return the pre-implementation change database path for aggregate history reads."""
    return _LEGACY_DB


# ---------------------------------------------------------------------------
# Context variables — callers set these before each LLM call
# ---------------------------------------------------------------------------

LLM_SKILL: ContextVar[str] = ContextVar("LLM_SKILL", default="unknown")
LLM_ACCOUNT: ContextVar[str] = ContextVar("LLM_ACCOUNT", default="unknown")

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def set_skill_context(skill: str, account: str = "unknown") -> None:
    """Set the skill and account tags for subsequent LLM calls in this context."""
    LLM_SKILL.set(skill)
    LLM_ACCOUNT.set(account)


def init_db(path: Path | None = None) -> None:
    """Create the llm_calls table in the DB at *path* (default: DB_PATH).

    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    Creates parent directories as needed.
    """
    target = path if path is not None else get_db_path()
    # Preserve the old filename migration within the legacy directory. Moving
    # it directly to *target* could cross filesystems after implementation change and would
    # also override an explicit FIELDKIT_LLM_LOG choice.
    if _OLD_DB.exists() and not _LEGACY_DB.exists():
        try:
            _OLD_DB.rename(_LEGACY_DB)
            logger.info("llm_log: migrated %s → %s", _OLD_DB.name, _LEGACY_DB.name)
        except OSError as exc:
            logger.warning("llm_log: could not auto-migrate old LLM log DB: %s", exc)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                id           TEXT PRIMARY KEY,
                ts           TEXT NOT NULL,
                skill        TEXT,
                account      TEXT,
                model        TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                latency_ms   INTEGER,
                cost_usd     REAL,
                error        TEXT,
                prompt_hash  TEXT
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Internal write helper
# ---------------------------------------------------------------------------


def _write_row(
    *,
    call_id: str,
    ts: str,
    skill: str,
    account: str,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int | None,
    cost_usd: float | None,
    error: str | None,
    prompt_hash: str | None,
) -> None:
    """Insert one row into llm_calls; silently swallows errors to avoid breaking callers.

    Thread-safe: acquires _write_lock before opening the connection to serialize
    concurrent writes from ThreadPoolExecutor workers (historic regression).
    """
    try:
        with _write_lock, sqlite3.connect(str(get_db_path())) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_calls
                  (id, ts, skill, account, model, input_tokens, output_tokens,
                   latency_ms, cost_usd, error, prompt_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    ts,
                    skill,
                    account,
                    model,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    cost_usd,
                    error,
                    prompt_hash,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_log: failed to write row: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Prompt hashing
# ---------------------------------------------------------------------------


def _hash_prompt(kwargs: dict[str, Any]) -> str | None:
    """Return SHA-256 hex of the raw prompt text, or None if unavailable."""
    try:
        messages = kwargs.get("messages") or []
        if isinstance(messages, list):
            text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        else:
            text = str(messages)
        return hashlib.sha256(text.encode()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# LiteLLM callback implementations
# ---------------------------------------------------------------------------


def _success_callback(kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
    """LiteLLM success_callback — writes one row per successful LLM call."""
    _ensure_initialized()
    try:
        slp: dict[str, Any] = kwargs.get("standard_logging_object") or {}

        # Prefer StandardLoggingPayload fields; fall back to raw kwargs
        model: str | None = slp.get("model") or kwargs.get("model")
        input_tokens: int | None = slp.get("prompt_tokens")
        output_tokens: int | None = slp.get("completion_tokens")
        cost_usd: float | None = slp.get("response_cost") if slp else kwargs.get("response_cost")

        # Latency: use response_time (seconds) from SLP if available, else derive from timestamps
        response_time_s: float | None = slp.get("response_time")
        if response_time_s is not None:
            latency_ms: int | None = int(response_time_s * 1000)
        elif isinstance(start_time, datetime) and isinstance(end_time, datetime):
            latency_ms = int((end_time - start_time).total_seconds() * 1000)
        else:
            latency_ms = None

        call_id = (
            slp.get("id")
            or (getattr(response_obj, "id", None) if response_obj is not None else None)
            or str(uuid.uuid4())
        )

        _write_row(
            call_id=call_id,
            ts=datetime.now(UTC).isoformat(),
            skill=LLM_SKILL.get(),
            account=LLM_ACCOUNT.get(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            error=None,
            prompt_hash=_hash_prompt(kwargs),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_log: success_callback error: %s", exc, exc_info=True)


def _failure_callback(
    kwargs: dict[str, Any],
    response_obj: Any,  # LiteLLM passes this; always None on failure path
    start_time: float,  # LiteLLM passes this; reserved for future latency calc
    end_time: float,  # LiteLLM passes this; reserved for future latency calc
) -> None:
    """LiteLLM failure_callback — writes one row per failed LLM call.

    LiteLLM dispatches: callback(kwargs, response_obj, start_time, end_time)
    via custom_logger.py:508-513. The exception is available as
    kwargs.get("exception"), injected by _failure_handler_helper_fn before
    dispatch. Matches the 4-arg signature of the sibling _success_callback.

    historic regression: previously accepted only 2 args → TypeError silently swallowed.
    """
    _ensure_initialized()
    try:
        model: str | None = kwargs.get("model")
        exception = kwargs.get("exception")
        error_str = str(exception) if exception is not None else "unknown error"

        _write_row(
            call_id=str(uuid.uuid4()),
            ts=datetime.now(UTC).isoformat(),
            skill=LLM_SKILL.get(),
            account=LLM_ACCOUNT.get(),
            model=model,
            input_tokens=None,
            output_tokens=None,
            latency_ms=None,
            cost_usd=None,
            error=error_str,
            prompt_hash=_hash_prompt(kwargs),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_log: failure_callback error: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Lazy init (historic regression): defer DB creation and litellm registration to first use.
# Previously this ran at module import time, costing ~50ms on every `fieldkit --help`.
# ---------------------------------------------------------------------------

_init_lock = _threading.Lock()
_init_state: list[bool] = [False]  # mutable container avoids PLW0603 global warning

# historic regression: serialize concurrent _write_row calls from ThreadPoolExecutor workers.
# SQLite WAL mode handles concurrent reads but concurrent writes from multiple threads
# can produce OperationalError: database is locked. A process-local lock serializes
# all writes through a single thread, eliminating the contention entirely.
_write_lock = _threading.Lock()


def _ensure_initialized() -> None:
    """Initialize DB and register litellm callbacks on first LLM callback fire.

    Thread-safe via double-checked locking — multiple parallel ingest workers may
    fire their first LLM callback simultaneously. init_db() is called exactly once.
    """
    if _init_state[0]:
        return
    with _init_lock:
        if _init_state[0]:
            return
        if not llm_disabled():
            init_db()
            try:
                import litellm

                logging.getLogger("LiteLLM").setLevel(logging.WARNING)
                logging.getLogger("LiteLLM Router").setLevel(logging.WARNING)
                if _success_callback not in litellm.success_callback:
                    litellm.success_callback.append(_success_callback)
                if _failure_callback not in litellm.failure_callback:
                    litellm.failure_callback.append(_failure_callback)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("llm_log: failed to register litellm callbacks: %s", _exc, exc_info=True)
        _init_state[0] = True
