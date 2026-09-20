"""tests/test_llm_log.py — Unit tests for lib/llm_log.py.

All five behaviours from the T03 plan:
  1. init_db() creates the llm_calls table in a temp directory.
  2. With NO_LLM=1, importing lib.llm still initialises the DB (side-effect).
  3. set_skill_context() sets context vars; values appear in a logged row.
  4. _success_callback writes a row with all correct field names and values.
  5. Prompt text is NOT stored — only a 64-char SHA-256 hex hash.

Tests set FIELDKIT_LLM_LOG env var via monkeypatch to redirect writes to a temp
file, so nothing touches ~/.local/share/fieldkit/llm_calls.db during the test run.
"""

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import fieldkit.llm.log as llm_log
from fieldkit.config import ConfigError
from fieldkit.llm.log import (
    _ensure_initialized,
    _failure_callback,
    _init_state,
    _success_callback,
    _write_row,
    init_db,
    set_skill_context,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_rows(db_path: Path) -> list[dict[str, Any]]:
    """Return all rows from llm_calls as dicts."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls").fetchall()]


def _make_response_obj(
    call_id: str = "test-call-id",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> MagicMock:
    """Build a minimal fake litellm ModelResponse."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    resp = MagicMock()
    resp.id = call_id
    resp.usage = usage
    resp.model = "vertex_ai/claude-3-5-sonnet@20241022"
    return resp


def _make_kwargs(
    model: str = "vertex_ai/claude-3-5-sonnet@20241022",
    prompt: str = "Hello, world!",
    response_cost: float = 0.0042,
    slp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal fake litellm kwargs dict."""
    messages = [{"role": "user", "content": prompt}]
    standard_logging_object: dict[str, Any] = (
        slp
        if slp is not None
        else {
            "id": "slp-call-id",
            "model": model,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "response_cost": response_cost,
            "response_time": 1.234,  # seconds
            "startTime": 1_700_000_000.0,
            "endTime": 1_700_000_001.234,
        }
    )
    return {
        "model": model,
        "messages": messages,
        "response_cost": response_cost,
        "standard_logging_object": standard_logging_object,
    }


# ---------------------------------------------------------------------------
# Fixture: isolated DB per test
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect llm_log writes to a temp file via FIELDKIT_LLM_LOG and re-init schema."""
    db = tmp_path / "test_llm_calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db))
    init_db(db)
    return db


@pytest.fixture(autouse=True)
def _approved_test_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep per-test FIELDKIT_LLM_LOG overrides under an approved data root."""
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))


def test_get_db_path_returns_existing_default_when_override_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)

    resolved_path = llm_log.get_db_path()

    assert resolved_path == llm_log.get_fieldkit_data() / "llm-calls.db"


def test_get_db_path_accepts_override_under_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "logs" / "llm-calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(override))

    resolved_path = llm_log.get_db_path()

    assert resolved_path == override.resolve()


@pytest.mark.parametrize("override", ["", "   ", "relative/llm-calls.db", "/tmp/llm-calls.db "])
def test_get_db_path_rejects_empty_or_relative_override(override: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDKIT_LLM_LOG", override)

    with pytest.raises(ConfigError, match="FIELDKIT_LLM_LOG"):
        llm_log.get_db_path()


def test_get_db_path_translates_unknown_user_override_to_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDKIT_LLM_LOG", "~fieldkit-user-that-does-not-exist/llm-calls.db")

    with pytest.raises(ConfigError, match="FIELDKIT_LLM_LOG"):
        llm_log.get_db_path()


def test_get_db_path_rejects_override_outside_approved_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = tmp_path.parent / "outside" / "llm-calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(rejected))

    with pytest.raises(ConfigError, match="not under an approved fieldkit root"):
        llm_log.get_db_path()

    assert not rejected.exists()


def test_get_db_path_rejects_approved_root_prefix_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = tmp_path.with_name(f"{tmp_path.name}-outside") / "llm-calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(rejected))

    with pytest.raises(ConfigError, match="not under an approved fieldkit root"):
        llm_log.get_db_path()


def test_get_db_path_rejects_symlink_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path.parent / "symlink-target"
    outside.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    rejected = link / "llm-calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(rejected))

    with pytest.raises(ConfigError, match="not under an approved fieldkit root"):
        llm_log.get_db_path()

    assert not (outside / "llm-calls.db").exists()


def test_get_db_path_enforces_stable_roots_without_workspace_config(monkeypatch: pytest.MonkeyPatch) -> None:
    stable_path = Path.home() / ".config" / "fieldkit" / "llm-calls.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(stable_path))
    monkeypatch.setattr(llm_log, "get_fieldkit_home", MagicMock(side_effect=ConfigError("missing")))
    monkeypatch.setattr(llm_log, "get_fieldkit_data", MagicMock(side_effect=ConfigError("missing")))

    resolved_path = llm_log.get_db_path()

    assert resolved_path == stable_path.resolve()


def test_get_db_path_ignores_unresolvable_optional_configured_root(monkeypatch: pytest.MonkeyPatch) -> None:
    stable_path = Path.home() / ".local" / "share" / "fieldkit" / "llm-calls.db"
    broken_root = MagicMock(spec=Path)
    broken_root.resolve.side_effect = RuntimeError("symlink loop")
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(stable_path))
    monkeypatch.setattr(llm_log, "get_fieldkit_home", MagicMock(return_value=broken_root))
    monkeypatch.setattr(llm_log, "get_fieldkit_data", MagicMock(side_effect=ConfigError("missing")))

    resolved_path = llm_log.get_db_path()

    assert resolved_path == stable_path.resolve()


# ---------------------------------------------------------------------------
# 1. init_db() creates the table in a temp directory
# ---------------------------------------------------------------------------


# ── TestInitDb (flattened) ──────────────────────────────────────────────────


def test_init_db_creates_table_and_parent_dirs(tmp_path: Path) -> None:
    """init_db() creates the llm_calls table and any missing parent dirs."""
    nested = tmp_path / "a" / "b" / "llm.db"
    assert not nested.exists()

    init_db(nested)

    assert nested.exists()
    with sqlite3.connect(str(nested)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "llm_calls" in tables


def test_init_db_all_columns_present(tmp_path: Path) -> None:
    """init_db() creates all 11 required columns."""
    db = tmp_path / "cols.db"
    init_db(db)
    with sqlite3.connect(str(db)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
    expected = {
        "id",
        "ts",
        "skill",
        "account",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "cost_usd",
        "error",
        "prompt_hash",
    }
    assert expected == cols


def test_init_db_idempotent(tmp_path: Path) -> None:
    """Calling init_db() twice does not raise and does not duplicate the table."""
    db = tmp_path / "idem.db"
    init_db(db)
    init_db(db)  # must not raise
    with sqlite3.connect(str(db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='llm_calls'").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 2. NO_LLM=1 — importing lib.llm_log must NOT touch the filesystem
# ---------------------------------------------------------------------------


# ── TestNoLLMSideEffect (flattened) ─────────────────────────────────────────


def test_no_llm_side_effect_no_db_created_under_no_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO_LLM=1, reloading lib.llm_log does NOT create the DB file."""
    import importlib

    db = tmp_path / "no_llm_should_not_exist.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db))
    monkeypatch.setenv("NO_LLM", "1")

    importlib.reload(llm_log)

    assert not db.exists(), f"DB file was created despite NO_LLM=1: {db}"


def test_no_llm_side_effect_no_filesystem_touch_with_nonexistent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NO_LLM=1 and a nonexistent HOME, reloading lib.llm_log creates no files."""
    import importlib

    fake_home = "/nonexistent_path_xyz_no_exist"
    db_under_fake_home = fake_home + "/.fieldkit/llm_calls.db"
    monkeypatch.setenv("NO_LLM", "1")
    monkeypatch.setenv("HOME", fake_home)
    monkeypatch.setenv("FIELDKIT_LLM_LOG", db_under_fake_home)

    # Must not raise and must not touch the filesystem
    importlib.reload(llm_log)

    from pathlib import Path as _Path

    assert not _Path(db_under_fake_home).exists(), "DB created despite NO_LLM=1 + nonexistent HOME"


def test_no_llm_side_effect_no_llm_callbacks_not_registered_in_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under NO_LLM=1, litellm callback registration is skipped via llm_disabled()."""
    import inspect

    src = inspect.getsource(llm_log)
    # historic regression: callback registration is now gated by llm_disabled() from fieldkit.config,
    # not by a direct os.environ.get("NO_LLM") check. Verify the guard is present.
    assert "llm_disabled()" in src, (
        "llm_log must gate litellm callback registration on llm_disabled() (historic regression: FIELDKIT_NO_LLM must be respected)"
    )
    # Also verify init_db() call is inside the guard, not outside it.
    guard_idx = src.index("if not llm_disabled()")
    init_db_idx = src.rindex("init_db()")  # last occurrence = inside the guard
    assert init_db_idx > guard_idx, "module-level init_db() must appear after the llm_disabled() guard"


# ---------------------------------------------------------------------------
# 3. set_skill_context() — context vars set correctly; appear in logged row
# ---------------------------------------------------------------------------


# ── TestSetSkillContext (flattened) ─────────────────────────────────────────


def test_set_skill_context_sets_both_context_vars() -> None:
    """set_skill_context() updates LLM_SKILL and LLM_ACCOUNT."""
    set_skill_context("meeting-prep", "GlobalPay")
    assert llm_log.LLM_SKILL.get() == "meeting-prep"
    assert llm_log.LLM_ACCOUNT.get() == "GlobalPay"


def test_set_skill_context_account_defaults_to_unknown() -> None:
    """set_skill_context() with only skill arg defaults account to 'unknown'."""
    set_skill_context("forecast")
    assert llm_log.LLM_SKILL.get() == "forecast"
    assert llm_log.LLM_ACCOUNT.get() == "unknown"


def test_set_skill_context_context_vars_appear_in_logged_row(tmp_db: Path) -> None:
    """Context var values are written into the skill/account columns."""
    set_skill_context("qbr-prep", "Acme")

    kwargs = _make_kwargs()
    response_obj = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, response_obj, now, now)

    rows = _db_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["skill"] == "qbr-prep"
    assert rows[0]["account"] == "Acme"


# ---------------------------------------------------------------------------
# 4. _success_callback writes a row with correct field values
# ---------------------------------------------------------------------------


# ── TestSuccessCallback (flattened) ─────────────────────────────────────────


def test_success_callback_writes_one_row(tmp_db: Path) -> None:
    """_success_callback inserts exactly one row per call."""
    set_skill_context("test-skill", "test-account")
    kwargs = _make_kwargs()
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    rows = _db_rows(tmp_db)
    assert len(rows) == 1


def test_success_callback_correct_token_counts(tmp_db: Path) -> None:
    """input_tokens and output_tokens come from StandardLoggingPayload."""
    set_skill_context("tok-test", "tok-account")
    slp = {
        "id": "tok-id",
        "model": "vertex_ai/claude-3-5-sonnet@20241022",
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
        "response_cost": 0.001,
        "response_time": 0.5,
    }
    kwargs = _make_kwargs(slp=slp)
    resp = _make_response_obj(prompt_tokens=42, completion_tokens=17)
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    assert row["input_tokens"] == 42
    assert row["output_tokens"] == 17


def test_success_callback_latency_from_response_time(tmp_db: Path) -> None:
    """latency_ms is derived from SLP response_time (seconds → ms)."""
    set_skill_context("lat-test", "lat-account")
    slp = {
        "id": "lat-id",
        "model": "m",
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
        "response_cost": 0.0,
        "response_time": 2.5,  # seconds
    }
    kwargs = _make_kwargs(slp=slp)
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    assert row["latency_ms"] == 2500


def test_success_callback_cost_usd_from_slp(tmp_db: Path) -> None:
    """cost_usd comes from StandardLoggingPayload.response_cost."""
    set_skill_context("cost-test", "cost-account")
    slp = {
        "id": "cost-id",
        "model": "m",
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
        "response_cost": 0.0042,
        "response_time": 1.0,
    }
    kwargs = _make_kwargs(response_cost=0.0042, slp=slp)
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    assert abs(row["cost_usd"] - 0.0042) < 1e-9


def test_success_callback_error_column_is_null_on_success(tmp_db: Path) -> None:
    """error column is NULL for successful calls."""
    set_skill_context("null-err", "null-account")
    kwargs = _make_kwargs()
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    assert row["error"] is None


def test_success_callback_ts_is_iso8601(tmp_db: Path) -> None:
    """ts column is a valid ISO-8601 UTC timestamp string."""
    set_skill_context("ts-test", "ts-account")
    kwargs = _make_kwargs()
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    # Should parse without error
    parsed = datetime.fromisoformat(row["ts"])
    assert parsed.tzinfo is not None


def test_success_callback_latency_fallback_from_datetime_diff(tmp_db: Path) -> None:
    """When SLP has no response_time, latency is derived from start/end datetime."""
    set_skill_context("dt-lat", "dt-account")
    # SLP without response_time
    slp: dict[str, Any] = {
        "id": "dt-id",
        "model": "m",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "response_cost": 0.0,
        # no response_time key
    }
    kwargs = _make_kwargs(slp=slp)
    resp = _make_response_obj()
    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2024, 1, 1, 0, 0, 3, tzinfo=UTC)  # 3 seconds later

    _success_callback(kwargs, resp, start, end)

    row = _db_rows(tmp_db)[0]
    assert row["latency_ms"] == 3000


# ---------------------------------------------------------------------------
# 5. Prompt text NOT stored — only SHA-256 hash
# ---------------------------------------------------------------------------


# ── TestPromptHashing (flattened) ───────────────────────────────────────────


def test_hash_prompt_prompt_hash_is_64_char_hex(tmp_db: Path) -> None:
    """prompt_hash is a 64-character lowercase hex string (SHA-256)."""
    set_skill_context("hash-test", "hash-account")
    kwargs = _make_kwargs(prompt="This is a secret prompt.")
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    ph = row["prompt_hash"]
    assert isinstance(ph, str)
    assert len(ph) == 64
    assert all(c in "0123456789abcdef" for c in ph)


def test_hash_prompt_prompt_text_not_stored(tmp_db: Path) -> None:
    """Raw prompt text does not appear anywhere in the stored row."""
    secret_prompt = "Top secret prompt content — must not be persisted."
    set_skill_context("secret-test", "secret-account")
    kwargs = _make_kwargs(prompt=secret_prompt)
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    row = _db_rows(tmp_db)[0]
    row_text = " ".join(str(v) for v in row.values() if v is not None)
    assert secret_prompt not in row_text


def test_hash_prompt_prompt_hash_matches_sha256(tmp_db: Path) -> None:
    """prompt_hash matches the SHA-256 of the raw prompt content."""
    prompt = "Reproducible prompt."
    set_skill_context("sha-test", "sha-account")
    kwargs = _make_kwargs(prompt=prompt)
    resp = _make_response_obj()
    now = datetime.now(UTC)

    _success_callback(kwargs, resp, now, now)

    expected = hashlib.sha256(prompt.encode()).hexdigest()
    row = _db_rows(tmp_db)[0]
    assert row["prompt_hash"] == expected


def test_hash_prompt_failure_callback_also_hashes_prompt(tmp_db: Path) -> None:
    """_failure_callback stores a prompt_hash, not raw prompt text."""
    prompt = "Failing prompt text."
    set_skill_context("fail-hash", "fail-account")
    kwargs = _make_kwargs(prompt=prompt)

    exc = RuntimeError("network timeout")
    kwargs["exception"] = exc
    _failure_callback(kwargs, None, 0.0, 1.0)

    rows = _db_rows(tmp_db)
    assert len(rows) == 1
    ph = rows[0]["prompt_hash"]
    assert len(ph) == 64
    assert prompt not in (rows[0].get("error") or "")


# ---------------------------------------------------------------------------
# Regression: env var override works after import
# ---------------------------------------------------------------------------


# ── TestEnvVarOverrideAfterImport (flattened) ───────────────────────────────


def test_env_var_override_after_import_write_row_routes_to_env_path_set_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_row() respects FIELDKIT_LLM_LOG even when set after module import."""
    # llm_log was imported at top of file (module already loaded).
    # Now set the env var to a new temp path.
    custom_db = tmp_path / "post_import_override.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(custom_db))

    # Initialize the schema at the custom path.
    init_db(custom_db)

    # Write a row — _write_row() calls _get_db_path() internally at write time.
    _write_row(
        call_id="regression-test-id",
        ts=datetime.now(UTC).isoformat(),
        skill="regression-skill",
        account="regression-account",
        model="test-model",
        input_tokens=5,
        output_tokens=10,
        latency_ms=100,
        cost_usd=0.001,
        error=None,
        prompt_hash="a" * 64,
    )

    # Row must exist in the custom DB.
    rows = _db_rows(custom_db)
    assert len(rows) == 1
    assert rows[0]["id"] == "regression-test-id"
    assert rows[0]["skill"] == "regression-skill"


def test_env_var_override_after_import_write_row_does_not_touch_default_path_when_env_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When FIELDKIT_LLM_LOG is set, the default ~/.local/share/fieldkit/llm_calls.db is NOT written."""
    custom_db = tmp_path / "custom_only.db"
    default_db = tmp_path / "fake_default.db"

    # Point the default away from the real home dir to a controlled temp file.
    # We achieve this by overriding FIELDKIT_LLM_LOG to the custom path, which
    # means _get_db_path() never resolves to the real default.
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(custom_db))

    init_db(custom_db)

    _write_row(
        call_id="no-default-write-id",
        ts=datetime.now(UTC).isoformat(),
        skill="no-default",
        account="no-default",
        model="m",
        input_tokens=1,
        output_tokens=1,
        latency_ms=50,
        cost_usd=0.0,
        error=None,
        prompt_hash="b" * 64,
    )

    # custom_db must have the row
    rows = _db_rows(custom_db)
    assert len(rows) == 1

    # default_db (never init'd, never written) must not exist
    assert not default_db.exists()


def test_env_var_override_after_import_env_var_change_between_calls_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each _write_row() call reads FIELDKIT_LLM_LOG afresh; changing the var mid-session routes correctly."""
    db_a = tmp_path / "db_a.db"
    db_b = tmp_path / "db_b.db"

    # Write to db_a
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db_a))
    init_db(db_a)
    _write_row(
        call_id="call-to-a",
        ts=datetime.now(UTC).isoformat(),
        skill="s",
        account="a",
        model="m",
        input_tokens=1,
        output_tokens=1,
        latency_ms=10,
        cost_usd=0.0,
        error=None,
        prompt_hash="c" * 64,
    )

    # Switch env var to db_b, write again
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db_b))
    init_db(db_b)
    _write_row(
        call_id="call-to-b",
        ts=datetime.now(UTC).isoformat(),
        skill="s",
        account="b",
        model="m",
        input_tokens=2,
        output_tokens=2,
        latency_ms=20,
        cost_usd=0.0,
        error=None,
        prompt_hash="d" * 64,
    )

    rows_a = _db_rows(db_a)
    rows_b = _db_rows(db_b)

    assert len(rows_a) == 1
    assert rows_a[0]["id"] == "call-to-a"

    assert len(rows_b) == 1
    assert rows_b[0]["id"] == "call-to-b"


# ---------------------------------------------------------------------------
# Failure callback
# ---------------------------------------------------------------------------


# ── TestFailureCallback (flattened) ─────────────────────────────────────────


def test_failure_callback_writes_row_with_error(tmp_db: Path) -> None:
    """_failure_callback writes a row with the error message and NULL tokens."""
    set_skill_context("fail-skill", "fail-account")
    kwargs = _make_kwargs()

    exc = RuntimeError("connection refused")
    kwargs["exception"] = exc
    _failure_callback(kwargs, None, 0.0, 1.0)

    rows = _db_rows(tmp_db)
    assert len(rows) == 1
    row = rows[0]
    assert "connection refused" in row["error"]
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["skill"] == "fail-skill"
    assert row["account"] == "fail-account"


# ---------------------------------------------------------------------------
# exc_info on write-failure warning
# ---------------------------------------------------------------------------


# ── TestWriteRowFailureExcInfo (flattened) ──────────────────────────────────


def test_write_row_write_row_failure_warning_has_exc_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When sqlite3.connect raises, logger.warning is called with exc_info=True."""
    import logging
    import sqlite3

    import fieldkit.llm.log as llm_log

    db_path = tmp_path / "fail.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db_path))

    # Patch sqlite3.connect inside llm_log to raise on write
    def _bad_connect(path: object, **kw: object) -> object:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(llm_log.sqlite3, "connect", _bad_connect)

    with caplog.at_level(logging.WARNING, logger="fieldkit.llm.log"):
        _write_row(
            call_id="exc-info-test",
            ts="2026-01-01T00:00:00",
            skill="s",
            account="a",
            model="m",
            input_tokens=1,
            output_tokens=1,
            latency_ms=10,
            cost_usd=0.0,
            error=None,
            prompt_hash=None,
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a warning from _write_row on connect failure"
    assert warning_records[0].exc_info is not None, "Expected exc_info to be set on the _write_row failure warning"


# ---------------------------------------------------------------------------
# Module-level litellm callback registration failure (pragma: no cover removed)
# ---------------------------------------------------------------------------


# ── TestLitellmCallbackRegistrationFailure (flattened) ──────────────────────


def test_failure_callback_swallows_litellm_registration_error_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When litellm callback registration raises, the exception is swallowed and a warning is logged."""
    import importlib
    import logging
    import sys
    import types

    import fieldkit.llm.log as llm_log

    db_path = tmp_path / "reg_fail.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db_path))
    # Ensure NO_LLM is not set so the registration block executes
    monkeypatch.delenv("NO_LLM", raising=False)

    # Build a fake litellm module whose success_callback raises on access
    fake_litellm = types.ModuleType("litellm")

    class _RaisingList:
        """Raises RuntimeError when __contains__ or append is called."""

        def __contains__(self, item: object) -> bool:
            raise RuntimeError("litellm internal error during registration")

        def append(self, item: object) -> None:
            raise RuntimeError("litellm internal error during registration")

    fake_litellm.success_callback = _RaisingList()  # type: ignore[attr-defined]
    fake_litellm.failure_callback = _RaisingList()  # type: ignore[attr-defined]

    # Inject the fake litellm into sys.modules so the reload picks it up
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    with caplog.at_level(logging.WARNING, logger="fieldkit.llm.log"):
        # historic regression: registration is now lazy — trigger it by calling _ensure_initialized()
        # Reset state so the lazy init runs fresh
        importlib.reload(llm_log)
        _init_state[0] = False
        _ensure_initialized()

    # The exception must be swallowed — no re-raise
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a warning when litellm callback registration fails"
    assert any("failed to register" in r.message for r in warning_records), (
        f"Warning message should mention registration failure; got: {[r.message for r in warning_records]}"
    )


# ---------------------------------------------------------------------------
# historic regression Regression: callbacks registered before first LLM call fires
# ---------------------------------------------------------------------------


# ── TestCallbackRegistrationEager (flattened) ───────────────────────────────


def test_success_callback_ensure_initialized_called_from_synthesize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After calling synthesize() once, _success_callback is in litellm.success_callback."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    db_path = tmp_path / "callback-registration.db"
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(db_path))
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("NO_LLM", raising=False)

    # Build a minimal fake litellm that records callback registration
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.suppress_debug_info = False  # type: ignore[attr-defined]
    fake_litellm.success_callback = []  # type: ignore[attr-defined]
    fake_litellm.failure_callback = []  # type: ignore[attr-defined]

    litellm_logger = logging.getLogger("LiteLLM")
    litellm_router_logger = logging.getLogger("LiteLLM Router")
    monkeypatch.setattr(litellm_logger, "level", logging.INFO)
    monkeypatch.setattr(litellm_router_logger, "level", logging.INFO)

    # Mock litellm.completion to return a minimal response
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "stub response"

    fake_litellm.suppress_debug_info = False  # type: ignore[attr-defined]

    class _FakeLitellmExceptions:
        AuthenticationError = Exception
        RateLimitError = Exception
        Timeout = Exception
        ServiceUnavailableError = Exception

    fake_litellm.exceptions = _FakeLitellmExceptions  # type: ignore[attr-defined]
    fake_litellm.completion = MagicMock(return_value=mock_response)  # type: ignore[attr-defined]

    # Reset init state
    import fieldkit.llm.log as _log

    _log._init_state[0] = False

    exceptions_module = types.ModuleType("litellm.exceptions")
    exceptions_module.AuthenticationError = Exception  # type: ignore[attr-defined]
    exceptions_module.RateLimitError = Exception  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"litellm": fake_litellm, "litellm.exceptions": exceptions_module}):
        import importlib

        import fieldkit.llm.core as _core

        importlib.reload(_core)

        # Call synthesize — this should trigger _ensure_initialized()
        _core.synthesize("test prompt")

    # After synthesize(), callbacks must be registered
    from fieldkit.llm.log import _success_callback as _cb

    assert _cb in fake_litellm.success_callback, (
        "historic regression: _success_callback not registered in litellm.success_callback "
        "after synthesize() call. _ensure_initialized() was not called eagerly."
    )
    assert litellm_logger.level == logging.WARNING
    assert litellm_router_logger.level == logging.WARNING


def test_success_callback_fieldkit_no_llm_stubs_transcribe(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: FIELDKIT_NO_LLM=1 must stub transcribe(), not just synthesize()."""
    from pathlib import Path as _Path
    from unittest.mock import patch

    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    monkeypatch.delenv("NO_LLM", raising=False)

    with patch("fieldkit.llm._transcribe.llm_disabled", return_value=True):
        from fieldkit.llm._transcribe import _NO_LLM_STUB, transcribe

        result = transcribe(_Path("fake_audio.m4a"))
        assert result == _NO_LLM_STUB, (
            "historic regression: transcribe() did not return stub when FIELDKIT_NO_LLM=1. "
            "llm_disabled() check is missing or broken."
        )


def test_get_read_db_path_falls_back_to_legacy_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)
    new_root = tmp_path / "new"
    legacy = tmp_path / "legacy" / "llm-calls.db"
    legacy.parent.mkdir()
    legacy.touch()
    monkeypatch.setattr(llm_log, "get_fieldkit_data", lambda: new_root)
    monkeypatch.setattr(llm_log, "_LEGACY_DB", legacy)

    result = llm_log.get_read_db_path()

    assert result == legacy
    assert legacy.exists()


def test_get_read_db_path_prefers_new_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)
    new_root = tmp_path / "new"
    new_db = new_root / "llm-calls.db"
    new_root.mkdir()
    new_db.touch()
    legacy = tmp_path / "legacy.db"
    legacy.touch()
    monkeypatch.setattr(llm_log, "get_fieldkit_data", lambda: new_root)
    monkeypatch.setattr(llm_log, "_LEGACY_DB", legacy)

    result = llm_log.get_read_db_path()

    assert result == new_db


def test_get_read_db_path_does_not_fall_back_for_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "missing-override.db"
    legacy = tmp_path / "legacy.db"
    legacy.touch()
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(override))
    monkeypatch.setattr(llm_log, "_LEGACY_DB", legacy)

    result = llm_log.get_read_db_path()

    assert result == override.resolve()


def test_get_db_path_accepts_parent_data_root_from_isolated_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.opencode import _build_opencode_env

    parent_data = tmp_path / "parent-data"
    child_data = tmp_path / "worktree" / ".fieldkit-data"
    durable_db = parent_data / "driver" / "llm-runs" / "run.db"
    env = _build_opencode_env(1266, child_data, durable_db)
    monkeypatch.setenv("FIELDKIT_DATA_DIR", env["FIELDKIT_DATA_DIR"])
    monkeypatch.setenv("FIELDKIT_LLM_LOG", env["FIELDKIT_LLM_LOG"])
    monkeypatch.setattr(llm_log, "get_fieldkit_data", lambda: child_data)
    monkeypatch.setattr(llm_log, "get_configured_fieldkit_data", lambda: parent_data)

    result = llm_log.get_db_path()

    assert result == durable_db.resolve()
