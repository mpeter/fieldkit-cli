"""Unit tests for FIELDKIT_* env var aliases in fieldkit/llm/core.py (implementation note).

Tests the FIELDKIT_LLM_MODEL, FIELDKIT_NO_LLM, and FIELDKIT_VERTEX_LOCATION aliases
introduced in spec 031.

Mock strategy: ``import litellm`` at module level ensures litellm is in sys.modules
before any ``patch("litellm.completion", ...)`` context is entered.  This is required
because ``synthesize()`` uses a lazy import — ``patch("litellm.completion", ...)``
works only when the module object already exists in sys.modules.  The pre-import here
guarantees that even in fresh xdist workers the patch target is resolvable.
(Pattern consistent with test_llm_core.py which also pre-imports via the
env_without_no_llm pattern rather than sys.modules injection.)

``synthesize`` is imported outside the env-patching context because it is a module-level
function object — the import itself is a no-op after the first load, and
``os.environ`` reads happen at call time, so the env-patching context only needs to be
active during the ``synthesize("hello")`` call.
"""

import os
from unittest.mock import MagicMock, patch

import litellm  # noqa: F401  — ensures litellm is in sys.modules before any patch()
import pytest
from _env_helpers import hermetic_env

from fieldkit.llm import _NO_LLM_STUB, synthesize

pytestmark = pytest.mark.unit

# Build a base env without NO_LLM so model-routing tests exercise the live path.


def _make_mock_completion(model_capture: list[str]) -> MagicMock:
    """Return a litellm.completion mock that captures the model kwarg."""
    mock = MagicMock()
    mock.return_value.choices = [MagicMock(message=MagicMock(content="response"))]

    def _side_effect(*args: object, **kwargs: object) -> MagicMock:
        model_capture.append(str(kwargs.get("model", "")))
        return mock.return_value

    mock.side_effect = _side_effect
    return mock


# ---------------------------------------------------------------------------
# FIELDKIT_LLM_MODEL / LLM_MODEL
# ---------------------------------------------------------------------------


def test_fieldkit_llm_model_takes_precedence_over_llm_model() -> None:
    """FIELDKIT_LLM_MODEL wins when both are set."""
    captured: list[str] = []
    mock_completion = _make_mock_completion(captured)
    env = hermetic_env(FIELDKIT_LLM_MODEL="vertex_ai/foo", LLM_MODEL="vertex_ai/bar")

    with (
        patch.dict(os.environ, env, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello")

    assert result == "response"
    assert captured, "litellm.completion was not called"
    assert captured[0] == "vertex_ai/foo", f"Expected FIELDKIT_LLM_MODEL to win, got model={captured[0]!r}"


def test_llm_model_alias_works_when_prefixed_absent() -> None:
    """Unprefixed LLM_MODEL still works when FIELDKIT_LLM_MODEL is not set."""
    captured: list[str] = []
    mock_completion = _make_mock_completion(captured)
    env = hermetic_env(LLM_MODEL="vertex_ai/bar")

    with (
        patch.dict(os.environ, env, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello")

    assert result == "response"
    assert captured, "litellm.completion was not called"
    assert captured[0] == "vertex_ai/bar", f"Expected LLM_MODEL fallback, got model={captured[0]!r}"


def test_explicit_model_takes_precedence_over_environment() -> None:
    """The synthesize model argument wins over both environment aliases."""
    captured: list[str] = []
    mock_completion = _make_mock_completion(captured)
    env = hermetic_env(FIELDKIT_LLM_MODEL="vertex_ai/env", LLM_MODEL="vertex_ai/alias")

    with (
        patch.dict(os.environ, env, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello", model="vertex_ai/explicit")

    assert result == "response"
    assert captured == ["vertex_ai/explicit"]


# ---------------------------------------------------------------------------
# FIELDKIT_NO_LLM / NO_LLM stub
# ---------------------------------------------------------------------------


def test_fieldkit_no_llm_returns_stub() -> None:
    """FIELDKIT_NO_LLM=1 returns the stub and does not call litellm."""
    with patch.dict(os.environ, {"FIELDKIT_NO_LLM": "1"}, clear=True):
        result = synthesize("hello")
    assert isinstance(result, str)
    assert result == _NO_LLM_STUB, f"Expected stub, got {result!r}"


def test_no_llm_alias_still_returns_stub() -> None:
    """Unprefixed NO_LLM=1 still returns the stub."""
    with patch.dict(os.environ, {"NO_LLM": "1"}, clear=True):
        result = synthesize("hello")
    assert isinstance(result, str)
    assert result == _NO_LLM_STUB, f"Expected stub, got {result!r}"


# ---------------------------------------------------------------------------
# FIELDKIT_VERTEX_LOCATION / CLOUD_ML_REGION (implementation note)
# ---------------------------------------------------------------------------


def _make_location_capture_mock() -> tuple[MagicMock, list[str]]:
    """Return a litellm.completion mock that captures the vertex_location kwarg."""
    captured: list[str] = []
    mock = MagicMock()
    mock.return_value.choices = [MagicMock(message=MagicMock(content="response"))]

    def _side_effect(*args: object, **kwargs: object) -> MagicMock:
        captured.append(str(kwargs.get("vertex_location", "")))
        return mock.return_value

    mock.side_effect = _side_effect
    return mock, captured


def test_fieldkit_vertex_location_takes_precedence_over_cloud_ml_region() -> None:
    """FIELDKIT_VERTEX_LOCATION wins over CLOUD_ML_REGION when both are set."""
    mock_completion, captured = _make_location_capture_mock()
    env = {
        **hermetic_env(),
        "FIELDKIT_VERTEX_LOCATION": "us-west2",
        "CLOUD_ML_REGION": "us-central1",
    }

    with (
        patch.dict(os.environ, env, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello")

    assert result == "response"
    assert captured, "litellm.completion was not called"
    assert captured[0] == "us-west2", f"Expected FIELDKIT_VERTEX_LOCATION to win, got vertex_location={captured[0]!r}"


def test_fieldkit_vertex_location_skips_global() -> None:
    """FIELDKIT_VERTEX_LOCATION='global' falls through to next priority."""
    mock_completion, captured = _make_location_capture_mock()
    env = {
        **hermetic_env(),
        "FIELDKIT_VERTEX_LOCATION": "global",
        "CLOUD_ML_REGION": "europe-west4",
    }

    with (
        patch.dict(os.environ, env, clear=True),
        patch("litellm.completion", mock_completion),
    ):
        result = synthesize("hello")

    assert result == "response"
    assert captured, "litellm.completion was not called"
    assert captured[0] == "europe-west4", (
        f"Expected 'global' to be skipped and CLOUD_ML_REGION to win, got vertex_location={captured[0]!r}"
    )
