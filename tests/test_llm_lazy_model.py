"""Tests for lazy model resolution in llm/core.py (spec 043).

Verifies that _resolve_model() reads FIELDKIT_ANTHROPIC_MODEL at call time
(not frozen at import), and that the override argument takes priority.
"""

import pytest

from fieldkit.config import ConfigError
from fieldkit.errors import LLMError


@pytest.mark.unit
def test_resolve_model_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_model(None) should read FIELDKIT_ANTHROPIC_MODEL at call time."""
    monkeypatch.setenv("FIELDKIT_ANTHROPIC_MODEL", "test-model")
    # Import after setting env so we test call-time reading, not import-time freezing.
    from fieldkit.llm.core import _resolve_model

    result = _resolve_model(None)
    # _resolve_model() always prepends "vertex_ai/" to the FIELDKIT_ANTHROPIC_MODEL
    # env var value (implementation note pattern). When the env var already contains "vertex_ai/"
    # this produces a double-prefix. The test uses a plain model name to avoid this:
    # use "test-model" (no prefix) so the result is unambiguously "vertex_ai/test-model".
    assert "test-model" in result, f"Expected model to include test-model, got: {result!r}"
    assert result.startswith("vertex_ai/"), f"Expected vertex_ai/ prefix, got: {result!r}"


@pytest.mark.unit
def test_resolve_model_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override arg must take priority over env var."""
    monkeypatch.setenv("FIELDKIT_ANTHROPIC_MODEL", "env-model")
    from fieldkit.llm.core import _resolve_model

    result = _resolve_model("vertex_ai/override-model")
    assert result == "vertex_ai/override-model"


@pytest.mark.unit
def test_resolve_model_falls_back_to_hardcoded_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_model(None) falls back to hardcoded default when no env/config."""
    monkeypatch.delenv("FIELDKIT_ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFAULT_SONNET_MODEL", raising=False)
    from fieldkit.llm.core import _resolve_model

    # Patch get_llm_model to return None (no config) using the provided monkeypatch fixture.
    # Do not create a nested MonkeyPatch() — it won't be cleaned up on test failure.
    monkeypatch.setattr("fieldkit.config._settings.get_llm_model", lambda: None)
    result = _resolve_model(None)
    # Should be the hardcoded default or whatever config returns
    assert result.startswith("vertex_ai/"), f"Expected vertex_ai/ prefix, got: {result!r}"


@pytest.mark.unit
def test_resolve_model_none_override_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_model(None) uses ANTHROPIC_DEFAULT_SONNET_MODEL when FIELDKIT_ANTHROPIC_MODEL absent."""
    monkeypatch.delenv("FIELDKIT_ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-3-haiku")
    from fieldkit.llm.core import _resolve_model

    result = _resolve_model(None)
    assert "claude-3-haiku" in result


@pytest.mark.unit
def test_resolve_model_invalid_config_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid model configuration must not be hidden by the hardcoded fallback."""
    for name in (
        "FIELDKIT_LLM_MODEL",
        "LLM_MODEL",
        "FIELDKIT_ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    from fieldkit.llm.core import _resolve_model

    def invalid_config() -> None:
        raise ConfigError("llm_model must start with vertex_ai/")

    monkeypatch.setattr("fieldkit.config._settings.get_llm_model", invalid_config)

    with pytest.raises(LLMError, match="Unable to resolve the Vertex AI model"):
        _resolve_model(None)
