"""Audio transcription through an explicitly selected LiteLLM model.

Callers pass ``model=`` or set ``FIELDKIT_TRANSCRIBE_MODEL``; this wrapper has
no default provider and does not impose the synthesis route's Vertex-only model
prefix. Provider availability and credentials depend on the selected model.
``FIELDKIT_NO_LLM`` returns a deterministic stub before optional imports or
provider calls. The public import is ``fieldkit.llm.transcribe``.
"""

import logging
import os
from pathlib import Path

from fieldkit.config import llm_disabled
from fieldkit.config.optional_dependencies import LLM_IMPORT_ROOTS, require_optional_profile
from fieldkit.config.retry import transient_retry
from fieldkit.errors import FieldkitError

logger = logging.getLogger(__name__)

_NO_LLM_STUB = "[TRANSCRIBE STUB] FIELDKIT_NO_LLM=1 is set — no API call was made."
_TRANSCRIPTION_TIMEOUT_SECONDS = 90

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
                    Falls back to FIELDKIT_TRANSCRIBE_MODEL.
                    No default model is selected.
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

    resolved_model = model or os.environ.get("FIELDKIT_TRANSCRIBE_MODEL")
    if not resolved_model:
        raise TranscribeError(
            "No transcription model configured. Set FIELDKIT_TRANSCRIBE_MODEL to a "
            "transcription model supported by your approved provider, or pass model explicitly.",
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
                timeout=_TRANSCRIPTION_TIMEOUT_SECONDS,
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
