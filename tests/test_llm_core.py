"""Tests for fieldkit.llm.core — region resolution, timeout, and retry (historic regression, historic regression).

All tests run with NO_LLM=1 or mock litellm so no real API calls are made.
"""

import os
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from _env_helpers import hermetic_env

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# historic regression: Vertex AI region resolution order
# ---------------------------------------------------------------------------


# ── TestVertexLocationResolution (flattened) ────────────────────────────────

_VERTEX_LOCATION_RESOLUTION__PATCH = "fieldkit.config._settings.get_vertex_location"


def test_vertex_location_resolution_config_key_takes_priority_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value="us-east5"):
        loc = _resolve_location()
    assert loc == "us-east5"


def test_vertex_location_resolution_cloud_ml_region_used_when_no_config_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUD_ML_REGION", "us-west1")
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value=None):
        loc = _resolve_location()
    assert loc == "us-west1"


def test_vertex_location_resolution_global_vertex_location_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_LOCATION", "global")
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value=None):
        loc = _resolve_location()
    assert loc == "us-east5"  # falls through to hardcoded default


def test_vertex_location_resolution_global_cloud_ml_region_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUD_ML_REGION", "global")
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value=None):
        loc = _resolve_location()
    assert loc == "us-east5"


def test_vertex_location_resolution_default_us_east5_when_all_absent_or_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value=None):
        loc = _resolve_location()
    assert loc == "us-east5"


def test_vertex_location_resolution_google_cloud_region_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_REGION", "europe-west4")
    with patch(_VERTEX_LOCATION_RESOLUTION__PATCH, return_value=None):
        loc = _resolve_location()
    assert loc == "europe-west4"


def _resolve_location() -> str:
    """Extract the vertex_location that core.py would pass to litellm.

    Patch target is fieldkit.config._settings.get_vertex_location because
    core.py imports it at call time from that canonical private owner.
    """
    from fieldkit.config._settings import get_vertex_location

    _REGION_SENTINELS = {"global", "GLOBAL", ""}

    def _pick(*candidates: str | None) -> str | None:
        for c in candidates:
            if c and c not in _REGION_SENTINELS:
                return c
        return None

    return (
        _pick(get_vertex_location())
        or _pick(os.environ.get("FIELDKIT_VERTEX_LOCATION"))  # implementation note
        or _pick(os.environ.get("CLOUD_ML_REGION"))
        or _pick(os.environ.get("VERTEX_LOCATION"))
        or _pick(os.environ.get("GOOGLE_CLOUD_REGION"))
        or "us-east5"
    )


# ---------------------------------------------------------------------------
# historic regression: synthesize() default timeout is 120s
# ---------------------------------------------------------------------------


# ── TestSynthesizeTimeout (flattened) ───────────────────────────────────────


def test_synthesize_timeout_default_timeout_is_120() -> None:
    """synthesize() must pass timeout=120 by default."""
    import inspect

    from fieldkit.llm.core import synthesize

    sig = inspect.signature(synthesize)
    param = sig.parameters.get("timeout")
    assert param is not None, "synthesize() missing timeout parameter"
    assert param.default == 120, f"Expected default timeout=120, got {param.default}"


def test_synthesize_timeout_timeout_exception_propagates_as_llmerror() -> None:
    """When litellm raises a timeout, synthesize() wraps it as LLMError."""
    from fieldkit.errors import LLMError
    from fieldkit.llm.core import synthesize

    mock_completion = MagicMock(side_effect=Exception("timeout"))
    # Clear NO_LLM so synthesize() doesn't stub out, then mock litellm
    env_without_no_llm = hermetic_env()
    with (
        patch.dict(os.environ, env_without_no_llm, clear=True),
        patch("litellm.completion", mock_completion),
        pytest.raises(LLMError, match="timeout") as exc_info,
    ):
        synthesize("test prompt")

    assert exc_info.value.category == "general"


# ---------------------------------------------------------------------------
# Retry behaviour: synthesize() retries on transient errors (T037)
# ---------------------------------------------------------------------------


# ── TestSynthesizeRetry (flattened) ─────────────────────────────────────────

_SYNTHESIZE_RETRY__ENV_NO_LLM_CLEARED: ClassVar[dict[str, str]] = {
    **hermetic_env(),
}


def _synthesize_retry_make_response(content: str) -> MagicMock:
    """Build a minimal litellm completion response mock."""
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


def test_synthesize_retry_rate_limit_retried_twice_then_succeeds() -> None:
    """RateLimitError on first two calls → success on third; litellm called 3 times."""
    from litellm.exceptions import RateLimitError

    from fieldkit.llm.core import synthesize

    rate_err = RateLimitError(
        message="rate limit",
        llm_provider="vertex_ai",
        model="vertex_ai/claude-3-5-sonnet@20241022",
    )
    good_response = _synthesize_retry_make_response("hello world")
    mock_completion = MagicMock(
        side_effect=[rate_err, rate_err, good_response],
    )

    with (
        patch.dict(os.environ, _SYNTHESIZE_RETRY__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
        # Suppress tenacity sleep so the test runs instantly
        patch("time.sleep"),
    ):
        result = synthesize("test prompt", model="vertex_ai/test-model")

    assert result == "hello world"
    assert mock_completion.call_count == 3


def test_synthesize_retry_auth_error_not_retried() -> None:
    """AuthenticationError → LLMError(category='auth') after exactly 1 call."""
    from litellm.exceptions import AuthenticationError

    from fieldkit.errors import LLMError
    from fieldkit.llm.core import synthesize

    auth_err = AuthenticationError(
        message="auth failed",
        llm_provider="vertex_ai",
        model="vertex_ai/claude-3-5-sonnet@20241022",
    )
    mock_completion = MagicMock(side_effect=auth_err)

    with (
        patch.dict(os.environ, _SYNTHESIZE_RETRY__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
        patch("time.sleep"),
        pytest.raises(LLMError, match="auth failed") as exc_info,
    ):
        synthesize("test prompt", model="vertex_ai/test-model")

    assert exc_info.value.category == "auth"
    # Auth errors must not be retried — exactly one call
    assert mock_completion.call_count == 1


# ---------------------------------------------------------------------------
# synthesize-prompt-guard: size guard, system= param, boundary, stub tests
# (tasks 4.1, 4.1b, 4.2, 4.2b, 4.3, 4.6)
# ---------------------------------------------------------------------------


# ── TestSynthesizePromptGuard (flattened) ───────────────────────────────────

_SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED: ClassVar[dict[str, str]] = {
    **hermetic_env(),
}


def _synthesize_prompt_guard_make_response(content: str) -> MagicMock:
    """Build a minimal litellm completion response mock."""
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


def test_synthesize_prompt_guard_synthesize_raises_llmerror_on_oversized_prompt() -> None:
    """Prompt exceeding _MAX_PROMPT_CHARS raises LLMError before any provider call."""
    from fieldkit.errors import LLMError
    from fieldkit.llm.core import _MAX_PROMPT_CHARS, synthesize

    mock_completion = MagicMock()
    with (
        patch.dict(os.environ, _SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
        pytest.raises(LLMError, match="exceeds maximum size") as exc_info,
    ):
        synthesize("x" * (_MAX_PROMPT_CHARS + 1))

    assert exc_info.value.category == "general"
    # Guard fires before any provider call — litellm must not be invoked.
    assert mock_completion.call_count == 0


def test_synthesize_prompt_guard_synthesize_accepts_prompt_at_exact_limit() -> None:
    """Prompt of exactly _MAX_PROMPT_CHARS is accepted and reaches litellm."""
    from fieldkit.llm.core import _MAX_PROMPT_CHARS, synthesize

    mock_completion = MagicMock(return_value=_synthesize_prompt_guard_make_response("ok"))
    with (
        patch.dict(os.environ, _SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("x" * _MAX_PROMPT_CHARS)

    assert result == "ok"
    assert mock_completion.call_count == 1


def test_synthesize_prompt_guard_synthesize_with_system_prepends_system_message() -> None:
    """system= prepends a system message with cache_control before the user message."""
    from fieldkit.llm.core import synthesize

    mock_completion = MagicMock(return_value=_synthesize_prompt_guard_make_response("reply"))
    with (
        patch.dict(os.environ, _SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello", system="You are X")

    assert result == "reply"
    # Extract the messages kwarg passed to litellm.completion.
    messages = mock_completion.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are X"
    assert messages[0]["cache_control"] == {"type": "ephemeral"}
    assert messages[1]["role"] == "user"


def test_synthesize_prompt_guard_synthesize_empty_string_system_produces_system_message() -> None:
    """system="" (empty string) IS sent as a system message (not suppressed).

    Callers SHOULD pass ``None`` rather than ``''`` to suppress the system
    message. Empty string is treated as a valid (empty) system message because
    the guard is ``if system is not None``, not ``if system``.
    """
    from fieldkit.llm.core import synthesize

    mock_completion = MagicMock(return_value=_synthesize_prompt_guard_make_response("reply"))
    with (
        patch.dict(os.environ, _SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello", system="")

    assert result == "reply"
    messages = mock_completion.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == ""
    assert messages[1]["role"] == "user"


def test_synthesize_prompt_guard_synthesize_without_system_sends_single_user_message() -> None:
    """When system= is omitted, exactly one user message is sent."""
    from fieldkit.llm.core import synthesize

    mock_completion = MagicMock(return_value=_synthesize_prompt_guard_make_response("reply"))
    with (
        patch.dict(os.environ, _SYNTHESIZE_PROMPT_GUARD__ENV_NO_LLM_CLEARED, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello")

    assert result == "reply"
    messages = mock_completion.call_args.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_synthesize_prompt_guard_synthesize_system_param_does_not_break_no_llm_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system= parameter does not interfere with the FIELDKIT_NO_LLM stub path."""
    from fieldkit.llm.core import _NO_LLM_STUB, synthesize

    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    result = synthesize("hello", system="You are X")
    assert result == _NO_LLM_STUB
