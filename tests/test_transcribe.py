"""tests/test_transcribe.py — Unit tests for lib/transcribe.py.

Strategy mirrors test_llm.py: all tests run offline via NO_LLM=1 or by
injecting a fake litellm module so no real API key is required.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

import fieldkit.config.retry as retry_policy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_litellm(
    text: str = "mock transcript",
    auth_error: bool = False,
    rate_error: bool = False,
    generic_error: bool = False,
) -> types.ModuleType:
    """Build a minimal fake litellm module with transcription support."""
    mod = types.ModuleType("litellm")

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    exceptions_mod = types.ModuleType("litellm.exceptions")
    exceptions_mod.AuthenticationError = AuthenticationError
    exceptions_mod.RateLimitError = RateLimitError
    mod.exceptions = exceptions_mod
    mod.AuthenticationError = AuthenticationError
    mod.RateLimitError = RateLimitError

    def _transcription(model, file, **kwargs):
        if auth_error:
            raise AuthenticationError("bad key")
        if rate_error:
            raise RateLimitError("too many requests")
        if generic_error:
            raise RuntimeError("provider failure")
        response = MagicMock()
        response.text = text
        return response

    mod.transcription = _transcription
    return mod


# Use a neutral provider prefix in tests — vertex_ai/* is not a valid
# transcription provider (LiteLLM raises ValueError for it). Tests mock
# litellm.transcription so the string never reaches the real router.
_TEST_MODEL = "test-provider/test-transcribe-model"


def _reload_transcribe(monkeypatch, mock_litellm=None, env=None):
    """Reload lib.transcribe with controlled sys.modules and env.

    Unless env explicitly sets FIELDKIT_TRANSCRIBE_MODEL or the test is
    exercising the NO_LLM stub path, a test model is injected so the
    no-default-provider guard does not fire.
    """
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    # Ensure a model is configured for tests that exercise the live path.
    # Tests that want the no-model error path must set env explicitly.
    env_dict = env or {}
    stub_active = any(env_dict.get(k) for k in ("NO_LLM", "FIELDKIT_NO_LLM"))
    if not stub_active and "FIELDKIT_TRANSCRIBE_MODEL" not in env_dict and "TRANSCRIBE_MODEL" not in env_dict:
        monkeypatch.setenv("FIELDKIT_TRANSCRIBE_MODEL", _TEST_MODEL)

    if mock_litellm is not None:
        monkeypatch.setitem(sys.modules, "litellm", mock_litellm)
        monkeypatch.setitem(sys.modules, "litellm.exceptions", mock_litellm.exceptions)

    if "fieldkit.llm._transcribe" in sys.modules:
        del sys.modules["fieldkit.llm._transcribe"]
    if "fieldkit.transcribe" in sys.modules:
        del sys.modules["fieldkit.transcribe"]

    import fieldkit.llm._transcribe as mod

    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ── TestNoLLMStub (flattened) ───────────────────────────────────────────────


def test_no_llm_stub_no_llm_returns_stub(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_LLM", "1")
    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    tr = _reload_transcribe(monkeypatch, env={"NO_LLM": "1"})

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"fake audio")

    result = tr.transcribe(audio)
    assert result == tr._NO_LLM_STUB
    assert "litellm" not in sys.modules


def test_no_llm_stub_no_llm_empty_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_LLM", "")
    mock = _make_mock_litellm(text="real transcript")
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock, env={"NO_LLM": ""})

    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake audio")

    result = tr.transcribe(audio)
    assert result == "real transcript"


# ── TestFormatValidation (flattened) ────────────────────────────────────────


def test_format_validation_unsupported_format_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm()
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    bad_file = tmp_path / "recording.xyz"
    bad_file.write_bytes(b"data")

    with pytest.raises(tr.TranscribeError) as exc_info:
        tr.transcribe(bad_file)

    assert exc_info.value.category == "unsupported-format"
    assert ".xyz" in str(exc_info.value)


@pytest.mark.parametrize("ext", [".mp3", ".m4a", ".wav", ".webm", ".ogg", ".flac", ".mp4"])
def test_format_validation_supported_formats_accepted(monkeypatch, tmp_path, ext):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(text="ok")
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    audio = tmp_path / f"recording{ext}"
    audio.write_bytes(b"audio data")

    result = tr.transcribe(audio)
    assert result == "ok"


# ── TestErrorWrapping (flattened) ───────────────────────────────────────────


def test_error_wrapping_auth_error_wrapped(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(auth_error=True)
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"data")

    with pytest.raises(tr.TranscribeError) as exc_info:
        tr.transcribe(audio)

    assert exc_info.value.category == "auth"
    assert exc_info.value.original is not None


def test_error_wrapping_rate_limit_wrapped(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(rate_error=True)
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)
    monkeypatch.setattr(
        tr,
        "transient_retry",
        lambda predicate, logger: retry_policy.transient_retry(predicate, logger, wait_min=0, wait_max=0),
    )

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"data")

    with pytest.raises(tr.TranscribeError) as exc_info:
        tr.transcribe(audio)

    assert exc_info.value.category == "rate-limit"


def test_error_wrapping_generic_error_wrapped(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(generic_error=True)
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    audio = tmp_path / "test.wav"
    audio.write_bytes(b"data")

    with pytest.raises(tr.TranscribeError) as exc_info:
        tr.transcribe(audio)

    assert exc_info.value.category == "general"


# ── TestModelResolution (flattened) ─────────────────────────────────────────


def test_model_resolution_env_model_forwarded(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_TRANSCRIBE_MODEL", raising=False)

    captured: dict = {}
    mock = _make_mock_litellm(text="result")
    orig = mock.transcription

    def tracking(model, file, **kwargs):
        captured["model"] = model
        return orig(model=model, file=file, **kwargs)

    mock.transcription = tracking
    tr = _reload_transcribe(
        monkeypatch,
        mock_litellm=mock,
        env={"TRANSCRIBE_MODEL": "openai/whisper-1"},
    )

    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"data")
    tr.transcribe(audio)

    assert captured["model"] == "openai/whisper-1"


def test_model_resolution_no_model_configured_raises(monkeypatch, tmp_path):
    """When no model is set via arg or env, TranscribeError is raised."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("TRANSCRIBE_MODEL", raising=False)
    monkeypatch.delenv("FIELDKIT_TRANSCRIBE_MODEL", raising=False)

    mock = _make_mock_litellm(text="ok")
    # Bypass _reload_transcribe's auto-inject by reloading manually
    monkeypatch.setitem(sys.modules, "litellm", mock)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", mock.exceptions)
    if "fieldkit.llm._transcribe" in sys.modules:
        del sys.modules["fieldkit.llm._transcribe"]
    import fieldkit.llm._transcribe as tr

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"data")

    with pytest.raises(tr.TranscribeError, match="No transcription model configured") as exc_info:
        tr.transcribe(audio)
    assert exc_info.value.category == "general"


def test_model_resolution_env_model_used_when_set(monkeypatch, tmp_path):
    """FIELDKIT_TRANSCRIBE_MODEL is forwarded to litellm.transcription."""
    monkeypatch.delenv("NO_LLM", raising=False)

    captured: dict = {}
    mock = _make_mock_litellm(text="ok")
    orig = mock.transcription

    def tracking(model, file, **kwargs):
        captured["model"] = model
        return orig(model=model, file=file, **kwargs)

    mock.transcription = tracking
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"data")
    tr.transcribe(audio)

    assert captured["model"] == _TEST_MODEL


# ── TestReturnValue (flattened) ─────────────────────────────────────────────


def test_return_value_text_stripped(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_LLM", raising=False)
    mock = _make_mock_litellm(text="  hello world  ")
    tr = _reload_transcribe(monkeypatch, mock_litellm=mock)

    audio = tmp_path / "test.m4a"
    audio.write_bytes(b"data")

    result = tr.transcribe(audio)
    assert result == "hello world"
