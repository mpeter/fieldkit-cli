"""tests/test_llm.py — Unit tests for lib/llm.py (all 8 behaviours).

All tests run under NO_LLM=1 by default in CI; the mock strategy uses
monkeypatch.setitem(sys.modules, "litellm", mock_litellm) so that tests
exercising the litellm path can inject a fake provider without a real API key.

The NO_LLM stub test deliberately avoids importing litellm at all — verified
by asserting litellm is absent from sys.modules after the call.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

import fieldkit.config.retry as retry_policy
import fieldkit.llm.core as llm_core
from fieldkit.errors import LLMError

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_litellm(
    content: str = "mock response",
    auth_error: bool = False,
    rate_error: bool = False,
    generic_error: bool = False,
) -> types.ModuleType:
    """Build a minimal fake litellm module.

    Provides:
      - completion()  — returns a response-like object or raises the chosen error
      - AuthenticationError / RateLimitError — real subclasses of Exception
    """
    mod = types.ModuleType("litellm")

    # Exception classes must be real classes so `except AuthenticationError`
    # works inside synthesize().
    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    # Attach directly on litellm (legacy path) and on litellm.exceptions
    # (the path used by lib/llm.py via `from litellm.exceptions import ...`).
    mod.AuthenticationError = AuthenticationError
    mod.RateLimitError = RateLimitError

    exceptions_mod = types.ModuleType("litellm.exceptions")
    exceptions_mod.AuthenticationError = AuthenticationError
    exceptions_mod.RateLimitError = RateLimitError
    mod.exceptions = exceptions_mod

    def _completion(model, messages, **kwargs):
        if auth_error:
            raise AuthenticationError("bad key")
        if rate_error:
            raise RateLimitError("too many requests")
        if generic_error:
            raise RuntimeError("unexpected provider failure")
        # Build a response-like object
        message = MagicMock()
        message.content = content
        choice = MagicMock()
        choice.message = message
        response = MagicMock()
        response.choices = [choice]
        return response

    mod.completion = _completion
    return mod


def _reload_llm(monkeypatch, mock_litellm: types.ModuleType | None = None, env: dict | None = None):
    """Reload lib.llm with a controlled sys.modules and optional env overrides.

    Args:
        mock_litellm: If provided, inject as the fake litellm module.
        env:          Dict of env-var overrides to apply before reload.
    """
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    if mock_litellm is not None:
        monkeypatch.setitem(sys.modules, "litellm", mock_litellm)
        monkeypatch.setitem(sys.modules, "litellm.exceptions", mock_litellm.exceptions)

    # Force a fresh module load so env changes and sys.modules patches take effect.
    if "fieldkit.llm" in sys.modules:
        del sys.modules["fieldkit.llm"]

    import fieldkit.llm as llm_module

    return llm_module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ── TestNoLLMStub (flattened) ───────────────────────────────────────────────


def test_no_llm_stub_no_llm_set_returns_stub(monkeypatch):
    """NO_LLM=1 → returns _NO_LLM_STUB without importing litellm."""
    monkeypatch.setenv("NO_LLM", "1")
    # Ensure litellm is NOT in sys.modules before the call
    monkeypatch.delitem(sys.modules, "litellm", raising=False)

    llm = _reload_llm(monkeypatch, mock_litellm=None, env={"NO_LLM": "1"})

    result = llm.synthesize("hello")

    assert result == llm._NO_LLM_STUB
    # litellm must NOT have been imported as a side-effect
    assert "litellm" not in sys.modules, "litellm was imported despite NO_LLM=1"


def test_no_llm_stub_no_llm_empty_string_falls_through(monkeypatch):
    """NO_LLM='' (empty string) → treated as unset, falls through to litellm path."""
    monkeypatch.setenv("NO_LLM", "")
    mock = _make_mock_litellm(content="real response")
    llm = _reload_llm(monkeypatch, mock_litellm=mock, env={"NO_LLM": ""})

    result = llm.synthesize("hello")

    assert result == "real response"


# ── TestModelResolution (flattened) ─────────────────────────────────────────


def test_model_resolution_llm_model_env_forwarded_to_completion(monkeypatch):
    """LLM_MODEL env var is forwarded to litellm.completion as the model arg."""
    monkeypatch.delenv("NO_LLM", raising=False)
    # FIELDKIT_LLM_MODEL wins over LLM_MODEL (implementation note); without this the operator's
    # own setting shadows the fallback under test (historic regression).
    monkeypatch.delenv("FIELDKIT_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "vertex_ai/claude-sonnet-4-6")

    captured: dict = {}
    mock = _make_mock_litellm(content="env model response")

    original_completion = mock.completion

    def tracking_completion(model, messages, **kwargs):
        captured["model"] = model
        return original_completion(model=model, messages=messages, **kwargs)

    mock.completion = tracking_completion
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    llm.synthesize("test prompt")

    assert captured["model"] == "vertex_ai/claude-sonnet-4-6"


def test_model_resolution_non_vertex_env_rejected_before_completion(monkeypatch):
    """A model env var cannot route account data to a direct API provider."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o")
    mock = _make_mock_litellm()
    mock.completion = MagicMock(side_effect=AssertionError("provider must not be called"))
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    with pytest.raises(LLMError, match=r"must start with 'vertex_ai/'"):
        llm.synthesize("test prompt")

    mock.completion.assert_not_called()


def test_model_resolution_non_vertex_explicit_model_rejected(monkeypatch):
    """The explicit model argument cannot bypass Vertex-only routing."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    mock = _make_mock_litellm()
    mock.completion = MagicMock(side_effect=AssertionError("provider must not be called"))
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    with pytest.raises(LLMError, match=r"must start with 'vertex_ai/'"):
        llm.synthesize("test prompt", model="anthropic/claude-sonnet-4-6")

    mock.completion.assert_not_called()


def test_model_resolution_default_model_used_when_env_unset(monkeypatch):
    """When LLM_MODEL is not set, _DEFAULT_MODEL is used."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("FIELDKIT_LLM_MODEL", raising=False)
    monkeypatch.delenv("FIELDKIT_ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFAULT_SONNET_MODEL", raising=False)

    captured: dict = {}
    mock = _make_mock_litellm(content="default model response")
    original_completion = mock.completion

    def tracking_completion(model, messages, **kwargs):
        captured["model"] = model
        return original_completion(model=model, messages=messages, **kwargs)

    mock.completion = tracking_completion
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    llm.synthesize("test prompt")

    assert captured["model"] == llm._DEFAULT_MODEL
    assert llm._DEFAULT_MODEL.startswith("vertex_ai/claude")


# ── TestErrorWrapping (flattened) ───────────────────────────────────────────


def test_error_wrapping_auth_error_raises_llm_error(monkeypatch):
    """litellm.AuthenticationError → LLMError with category='auth'."""
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(auth_error=True)
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    with pytest.raises(LLMError) as exc_info:
        llm.synthesize("trigger auth error")

    err = exc_info.value
    assert err.category == "auth"
    assert "auth" in str(err).lower() or "authentication" in str(err).lower()
    assert err.original is not None


def test_error_wrapping_rate_limit_error_raises_llm_error(monkeypatch):
    """litellm.RateLimitError → LLMError with category='rate-limit'."""
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(rate_error=True)
    llm = _reload_llm(monkeypatch, mock_litellm=mock)
    monkeypatch.setattr(
        llm_core,
        "transient_retry",
        lambda predicate, logger: retry_policy.transient_retry(predicate, logger, wait_min=0, wait_max=0),
    )

    with pytest.raises(LLMError) as exc_info:
        llm.synthesize("trigger rate limit")

    err = exc_info.value
    assert err.category == "rate-limit"
    assert err.original is not None


def test_error_wrapping_generic_exception_raises_llm_error(monkeypatch):
    """Any unexpected exception → LLMError with category='general'."""
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(generic_error=True)
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    with pytest.raises(LLMError) as exc_info:
        llm.synthesize("trigger generic error")

    err = exc_info.value
    assert err.category == "general"
    assert err.original is not None


# ---------------------------------------------------------------------------
# implementation change regression: VERTEX_LOCATION and GOOGLE_CLOUD_REGION env var aliases
# ---------------------------------------------------------------------------


# ── TestVertexLocationEnvVarAliases (flattened) ─────────────────────────────


def _vertex_location_env_var_aliases_vertex_loc(monkeypatch, env_vars: dict) -> str | None:
    """Run synthesize() with given env vars; return the vertex_location kwarg."""
    captured: dict = {}
    mock = _make_mock_litellm()
    original = mock.completion

    def capturing(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    mock.completion = capturing
    for var in ("CLOUD_ML_REGION", "VERTEX_LOCATION", "GOOGLE_CLOUD_REGION"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NO_LLM", raising=False)
    llm = _reload_llm(monkeypatch, mock_litellm=mock)
    llm.synthesize("test")
    return captured.get("vertex_location")


def test_vertex_location_env_var_aliases_cloud_ml_region_still_works(monkeypatch) -> None:
    # historic regression: "global" is not a valid regional endpoint — skipped, falls to us-east5 default
    assert _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {"CLOUD_ML_REGION": "global"}) == "us-east5"


def test_vertex_location_env_var_aliases_cloud_ml_region_real_value_works(monkeypatch) -> None:
    assert _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {"CLOUD_ML_REGION": "us-central1"}) == "us-central1"


def test_vertex_location_env_var_aliases_vertex_location_env_var(monkeypatch) -> None:
    """VERTEX_LOCATION must set vertex_location kwarg (implementation change); 'global' skipped (historic regression)."""
    assert _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {"VERTEX_LOCATION": "global"}) == "us-east5"


def test_vertex_location_env_var_aliases_vertex_location_real_value_works(monkeypatch) -> None:
    assert (
        _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {"VERTEX_LOCATION": "europe-west4"}) == "europe-west4"
    )


def test_vertex_location_env_var_aliases_google_cloud_region_env_var(monkeypatch) -> None:
    """GOOGLE_CLOUD_REGION must set vertex_location kwarg (implementation change)."""
    assert (
        _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {"GOOGLE_CLOUD_REGION": "us-central1"})
        == "us-central1"
    )


def test_vertex_location_env_var_aliases_cloud_ml_region_priority(monkeypatch) -> None:
    result = _vertex_location_env_var_aliases_vertex_loc(
        monkeypatch, {"CLOUD_ML_REGION": "us-east5", "VERTEX_LOCATION": "global"}
    )
    assert result == "us-east5"


def test_vertex_location_env_var_aliases_no_region_var_uses_default(monkeypatch) -> None:
    # historic regression: when no region env var is set, defaults to "us-east5"
    assert _vertex_location_env_var_aliases_vertex_loc(monkeypatch, {}) == "us-east5"


# ── TestReturnValue (flattened) ─────────────────────────────────────────────


def test_return_value_return_value_is_string_from_response(monkeypatch):
    """synthesize() returns str(response.choices[0].message.content)."""
    monkeypatch.delenv("NO_LLM", raising=False)
    expected = "This is the model output."
    mock = _make_mock_litellm(content=expected)
    llm = _reload_llm(monkeypatch, mock_litellm=mock)

    result = llm.synthesize("what is the answer?")

    assert result == expected
    assert isinstance(result, str)
