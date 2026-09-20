"""lib/transcribe.py — Audio transcription via LiteLLM.

Thin wrapper around litellm.transcription. No default provider is configured —
callers must set FIELDKIT_TRANSCRIBE_MODEL (or pass model= explicitly) to a
supported transcription model string. Falls back to the NO_LLM stub path so
tests can run offline.

Provider validation (2026-07-10):
    vertex_ai/gemini-2.5-flash was evaluated as a candidate default. LiteLLM's
    transcription() function does NOT support the vertex_ai provider — it only
    routes to azure, openai, nvidia_riva, soniox, and OpenAI-compatible cloud
    providers. A vertex_ai transcription call raises ValueError:
    "Unmapped provider passed in." No local-whisper LiteLLM provider exists
    without a dedicated gRPC service or external API key.

    Conclusion: No default provider is configured. Callers must supply a model
    string explicitly or via FIELDKIT_TRANSCRIBE_MODEL. Supported option that
    complies with the no-external-API-key constraint:
      - nvidia_riva/<model>  (requires a local NVIDIA Riva gRPC server)
      Also requires: NVIDIA_RIVA_API_BASE=<host:port> (e.g. localhost:50051)
      and:          pip install 'litellm[stt-nvidia-riva]'  (riva-client)
    All cloud STT providers (OpenAI-compatible, Azure, soniox) require external
    API keys and are disallowed. Explicit operator configuration is required.

Usage:
    from fieldkit.transcribe import transcribe, TranscribeError

    text = transcribe(Path("recording.m4a"), model="nvidia_riva/<your-riva-model-name>")

Environment variables (implementation note: FIELDKIT_* prefixed names are primary):
    FIELDKIT_NO_LLM / NO_LLM — any non-empty string → return stub, skip API call
    FIELDKIT_TRANSCRIBE_MODEL / TRANSCRIBE_MODEL — required: set to a supported
        transcription model string (e.g. nvidia_riva/<model>). No default provider
        is configured; omitting this raises TranscribeError.
"""

import logging
import os
from pathlib import Path

from fieldkit.config import llm_disabled
from fieldkit.config.optional_dependencies import LLM_IMPORT_ROOTS, require_optional_profile
from fieldkit.config.retry import transient_retry
from fieldkit.errors import FieldkitError

logger = logging.getLogger(__name__)

_NO_LLM_STUB = "[TRANSCRIBE STUB] NO_LLM=1 is set — no API call was made."

# Supported audio formats (LiteLLM transcription API).
_SUPPORTED_EXTENSIONS = frozenset({".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".flac"})


class TranscribeError(FieldkitError):
    """Uniform error wrapper for transcription failures.

    Attributes:
        category: one of "auth", "rate-limit", "unsupported-format", "general"
        original: the underlying exception, if any
    """

    def __init__(
        self,
        message: str,
        category: str = "general",
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.original = original

    def __str__(self) -> str:
        return f"[TranscribeError/{self.category}] {super().__str__()}"


def transcribe(
    audio_path: Path,
    *,
    model: str | None = None,
    language: str = "en",
    prompt: str | None = None,
) -> str:
    """Transcribe an audio file to text.

    Args:
        audio_path: Path to the audio file (m4a, mp3, wav, etc.)
        model:      Optional model override (e.g. "openai/whisper-1").
                    Falls back to TRANSCRIBE_MODEL env var, then the default.
        language:   BCP-47 language code hint (default: "en").
        prompt:     Optional text to guide the transcription (speaker names,
                    domain terms). Improves accuracy on jargon.

    Returns:
        The full transcript as a plain string.

    Raises:
        TranscribeError: On unsupported format, auth failure, or API error.
    """
    if llm_disabled():
        return _NO_LLM_STUB

    ext = audio_path.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise TranscribeError(
            f"Unsupported audio format '{ext}'. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
            category="unsupported-format",
        )

    require_optional_profile("transcription", "llm", LLM_IMPORT_ROOTS)

    import litellm
    import openai
    from litellm.exceptions import AuthenticationError, RateLimitError

    # historic regression: Register audit-log callbacks eagerly before the transcription call.
    from fieldkit.llm.log import _ensure_initialized

    _ensure_initialized()

    # implementation note: FIELDKIT_TRANSCRIBE_MODEL is the primary name; TRANSCRIBE_MODEL is the legacy alias.
    resolved_model = model or os.environ.get("FIELDKIT_TRANSCRIBE_MODEL") or os.environ.get("TRANSCRIBE_MODEL")
    if not resolved_model:
        raise TranscribeError(
            "No transcription model configured. Set FIELDKIT_TRANSCRIBE_MODEL to a Google "
            "Vertex AI or local model string (e.g. vertex_ai/gemini-2.5-flash).",
            category="general",
        )

    # Build the retried transcription caller after lazy imports so the retry
    # decorator can reference the now-imported exception classes. Policy comes
    # from fieldkit.config.retry: 3 attempts, exponential backoff 2-10s,
    # per-attempt WARNING. Transcription is an idempotent read, hence
    # transient_retry. Auth errors are NOT retried.
    @transient_retry(lambda exc: isinstance(exc, (RateLimitError, openai.APIConnectionError)), logger)
    def _call_transcription_inner(p: Path, m: str) -> str:
        with p.open("rb") as f:
            response = litellm.transcription(
                model=m,
                file=f,
                language=language,
                prompt=prompt,
                response_format="text",
            )
        # litellm returns TranscriptionResponse; .text holds the plain string
        text = getattr(response, "text", None) or str(response)
        return text.strip()

    try:
        return _call_transcription_inner(audio_path, resolved_model)

    except AuthenticationError as exc:
        raise TranscribeError(
            f"Authentication failed for model '{resolved_model}': {exc}",
            category="auth",
            original=exc,
        ) from exc

    except RateLimitError as exc:
        raise TranscribeError(
            f"Rate limit exceeded for model '{resolved_model}': {exc}",
            category="rate-limit",
            original=exc,
        ) from exc

    except Exception as exc:
        raise TranscribeError(
            f"Transcription failed for '{audio_path.name}': {exc}",
            category="general",
            original=exc,
        ) from exc
