"""fieldkit.llm.core — Vendor-neutral LLM wrapper backed by LiteLLM.

Usage:
    from fieldkit.llm import synthesize
    from fieldkit.errors import LLMError

    # Normal path (uses LiteLLM via Vertex AI):
    result = synthesize("Summarize this account...")

    # Offline / test path (set NO_LLM=1 in env):
    result = synthesize("hello")  # returns _NO_LLM_STUB deterministically

Environment variables (implementation note: FIELDKIT_* prefixed names are primary; unprefixed are aliases):
    FIELDKIT_NO_LLM / NO_LLM         — any non-empty string → return stub, skip litellm entirely
    FIELDKIT_LLM_MODEL / LLM_MODEL   — override the default Vertex AI model (must use vertex_ai/)
    GOOGLE_CLOUD_PROJECT or VERTEXAI_PROJECT — GCP project for Vertex AI (required for live calls)
    FIELDKIT_VERTEX_LOCATION / CLOUD_ML_REGION / VERTEX_LOCATION / GOOGLE_CLOUD_REGION —
        Vertex AI region override. Priority (highest to lowest): config.yaml vertex_location,
        FIELDKIT_VERTEX_LOCATION, CLOUD_ML_REGION, VERTEX_LOCATION, GOOGLE_CLOUD_REGION.
        "global" is skipped as it is not a real regional endpoint. Default region: us-east5.
    FIELDKIT_ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_SONNET_MODEL — override the Anthropic model slug.

Authentication:
    All LLM calls route through Google Vertex AI using Application Default Credentials.
    Run ``gcloud auth application-default login`` before using live LLM calls.
    Direct Anthropic API keys (ANTHROPIC_API_KEY) are NOT used and NOT required.
"""

import contextlib
import logging
import os

from fieldkit.config import llm_disabled
from fieldkit.config.optional_dependencies import LLM_IMPORT_ROOTS, require_optional_profile
from fieldkit.config.retry import transient_retry
from fieldkit.errors import LLMError

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

_NO_LLM_STUB = "[LLM STUB] NO_LLM=1 is set — no API call was made."
# Default timeout for LLM synthesis calls (seconds).  Covers two Vertex AI cold-start
# budgets (30-60s each).  Callers that need a different budget pass timeout= explicitly.
LLM_SYNTHESIS_TIMEOUT = 90
# Hardcoded fallback model — used only when no override is provided via argument,
# environment variable, or config file. See _resolve_model() for the full priority chain.
_HARDCODED_DEFAULT_MODEL = "vertex_ai/claude-sonnet-4-6"
# Backward-compatible alias — callers that reference llm._DEFAULT_MODEL continue to work.
# The actual model used at runtime is determined by _resolve_model() inside synthesize().
_DEFAULT_MODEL = _HARDCODED_DEFAULT_MODEL
# Conservative initial value (~100k tokens). Revisit using audit log data once finding #1
# (LLM audit log fix) is implemented.
_MAX_PROMPT_CHARS: int = 400_000


def _require_vertex_model(model: str) -> str:
    """Return *model* when it uses Vertex AI routing; reject every other provider."""
    if not model.startswith("vertex_ai/"):
        raise LLMError(
            f"LLM model {model!r} must start with 'vertex_ai/' so fieldkit cannot route account data "
            "to a direct API provider.",
            category="general",
        )
    return model


def _select_model(override: str | None) -> str:
    """Select a model by precedence, reading environment and config at call time."""
    if override:
        return override

    model_env = os.environ.get("FIELDKIT_LLM_MODEL") or os.environ.get("LLM_MODEL")
    if model_env:
        return model_env

    # implementation note: FIELDKIT_ANTHROPIC_MODEL takes priority over ANTHROPIC_DEFAULT_SONNET_MODEL.
    anthropic_model_env = os.environ.get("FIELDKIT_ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
    if anthropic_model_env:
        # Strip model version brackets if present (e.g. "claude-3-5-sonnet@20241022[...]")
        return "vertex_ai/" + anthropic_model_env.strip("[]").split("[")[0]

    # Config file: llm_model key (validated to start with vertex_ai/ by get_llm_model()).
    try:
        from fieldkit.config._settings import get_llm_model

        config_model = get_llm_model()
        if config_model:
            return config_model
    except Exception as exc:
        raise LLMError(
            f"Unable to resolve the Vertex AI model from fieldkit configuration: {exc}",
            category="general",
            original=exc,
        ) from exc

    return _HARDCODED_DEFAULT_MODEL


def _resolve_model(override: str | None) -> str:
    """Resolve and validate the LLM model string at call time.

    G3d: Replaces module-level _DEFAULT_MODEL constant (which was frozen at import
    time) with a function called inside synthesize(). This allows test monkeypatching
    of FIELDKIT_ANTHROPIC_MODEL to work correctly — the env var is read fresh on
    each call rather than once at module import.

    Priority (first non-empty wins):
    1. ``override`` argument passed to ``synthesize()``.
    2. ``FIELDKIT_LLM_MODEL`` / ``LLM_MODEL`` env vars.
    3. ``FIELDKIT_ANTHROPIC_MODEL`` / ``ANTHROPIC_DEFAULT_SONNET_MODEL`` env vars.
    4. ``llm_model`` key in config.yaml (via ``get_llm_model()``).
    5. Hardcoded default ``vertex_ai/claude-sonnet-4-6``.

    Args:
        override: Explicit model string from the ``synthesize(model=...)`` argument,
            or ``None`` to use environment/config resolution.

    Returns:
        A LiteLLM-compatible model string starting with ``vertex_ai/``.

    Raises:
        LLMError: If an explicit or environment override selects a non-Vertex
            provider.
    """
    return _require_vertex_model(_select_model(override))


# ---------------------------------------------------------------------------
# Vertex AI region resolution (historic regression)
# ---------------------------------------------------------------------------

# Not real regional endpoints for Vertex AI Anthropic — always skip.
_REGION_SENTINELS: frozenset[str] = frozenset({"global", "GLOBAL", ""})


def _pick_region(*candidates: str | None) -> str | None:
    """Return the first candidate that is a valid (non-sentinel) region string."""
    for c in candidates:
        if c and c not in _REGION_SENTINELS:
            return c
    return None


def _resolve_vertex_location() -> str:
    """Resolve the Vertex AI region to pass to LiteLLM.

    Priority (highest to lowest):
    1. ``vertex_location`` key in ~/.config/fieldkit/config.yaml
    2. FIELDKIT_VERTEX_LOCATION env var (implementation note: prefixed alias)
    3. CLOUD_ML_REGION env var
    4. VERTEX_LOCATION env var  (skips "global" — not a real regional endpoint)
    5. GOOGLE_CLOUD_REGION env var
    6. Hardcoded default "us-east5"
    """
    # Lazy import: fieldkit.config depends on nothing but must not be imported
    # at module level here to keep the import graph clean. The import is fast
    # (cached via @cache on _load_raw_config) after the first call.
    from fieldkit.config._settings import get_vertex_location

    return (
        _pick_region(get_vertex_location())
        or _pick_region(os.environ.get("FIELDKIT_VERTEX_LOCATION"))  # implementation note
        or _pick_region(os.environ.get("CLOUD_ML_REGION"))
        or _pick_region(os.environ.get("VERTEX_LOCATION"))
        or _pick_region(os.environ.get("GOOGLE_CLOUD_REGION"))
        or "us-east5"
    )


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def synthesize(
    prompt: str,
    model: str | None = None,
    timeout: int = 120,
    *,
    system: str | None = None,
) -> str:
    """Send *prompt* to an LLM and return the response as a plain string.

    Args:
        prompt:  The user message to send.
        model:   Optional Vertex AI model override (e.g. "vertex_ai/claude-sonnet-4-6").
                 Falls back to LLM_MODEL env var, then _DEFAULT_MODEL.
        timeout: Request timeout in seconds forwarded to
                 ``litellm.completion(timeout=...)``.  Default 120s covers two
                 Vertex AI cold-start budgets.  Pass explicitly when you need a
                 shorter window (e.g. ``timeout=60`` for pipeline synthesis —
                 see commands/pipeline/main.py).
        system:  Optional system message. When provided, prepended as a
                 ``{"role": "system"}`` message with ``cache_control: {"type": "ephemeral"}``
                 for Anthropic prompt caching. When ``None`` (default), behaviour is
                 identical to the current single-user-message call. Pass ``None`` (not
                 ``""``) to suppress the system message. Callers MUST only pass static,
                 caller-controlled instruction strings — never user-supplied content.

    Returns:
        The model's reply as a string.

    Raises:
        LLMError: On any provider error (auth, rate-limit, timeout, or general
                  failure).
        LLMError: Also raised (category="general") if ``len(prompt)`` exceeds
                  ``_MAX_PROMPT_CHARS`` before any provider call is attempted.
    """
    # ------------------------------------------------------------------
    # 1. Stub path — checked before any litellm import
    #    implementation note: llm_disabled() reads both FIELDKIT_NO_LLM (primary) and NO_LLM (alias).
    # ------------------------------------------------------------------
    if llm_disabled():
        return _NO_LLM_STUB

    # ------------------------------------------------------------------
    # 1b. Prompt size guard — fires before litellm import so rejected
    #     prompts pay no import cost and the error is actionable.
    # ------------------------------------------------------------------
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise LLMError(
            f"Prompt exceeds maximum size ({len(prompt):,} chars > {_MAX_PROMPT_CHARS:,} limit). "
            "Truncate the input before calling synthesize().",
            category="general",
        )

    # ------------------------------------------------------------------
    # 2. Optional-profile guard and lazy import. The guard keeps base-install
    #    failures on fieldkit's actionable exit-3 path instead of exposing an
    #    internal ModuleNotFoundError traceback.
    # ------------------------------------------------------------------
    require_optional_profile("LLM-powered command", "llm", LLM_IMPORT_ROOTS)

    import litellm
    import openai
    from litellm.exceptions import AuthenticationError, RateLimitError

    # historic regression: Register audit-log callbacks eagerly so the very first LLM call
    # is recorded. Previously _ensure_initialized() was only called from inside
    # the callbacks themselves — meaning the first call's result was never logged.
    from fieldkit.llm.log import _ensure_initialized

    _ensure_initialized()

    # Suppress litellm's error-page footer spam (Provider List, feedback links)
    litellm.suppress_debug_info = True

    # G3e: Named constant for retryable exception types.
    # Defined inside synthesize() (after lazy import) so the exception classes
    # are available. Built as a tuple so the retry predicate can isinstance() against it.
    _RETRYABLE: tuple[type[BaseException], ...] = (RateLimitError, openai.APIConnectionError)
    # Extend with optional exception types that may not exist in all LiteLLM versions.
    with contextlib.suppress(AttributeError):
        _RETRYABLE = (*_RETRYABLE, litellm.exceptions.Timeout)
    with contextlib.suppress(AttributeError):
        _RETRYABLE = (*_RETRYABLE, litellm.exceptions.ServiceUnavailableError)

    # ------------------------------------------------------------------
    # 3. Model resolution: param > FIELDKIT_LLM_MODEL > LLM_MODEL > _resolve_model()
    #    implementation note: FIELDKIT_LLM_MODEL is the prefixed alias; LLM_MODEL is the fallback.
    #    G3d: _resolve_model() reads env at call time (not frozen at import).
    # ------------------------------------------------------------------
    resolved_model = _resolve_model(model)

    # ------------------------------------------------------------------
    # 4. Build the retried litellm caller (defined here so the retry
    #    decorator can reference the now-imported exception classes).
    #    Policy comes from fieldkit.config.retry: 3 attempts, exponential
    #    backoff 2-10s, per-attempt WARNING. An LLM completion is an
    #    idempotent read, hence transient_retry. Auth errors are NOT
    #    retried — they are caught in the outer try/except below.
    # ------------------------------------------------------------------
    @transient_retry(lambda exc: isinstance(exc, _RETRYABLE), logger)
    def _call_litellm_inner(p: str, m: str, t: int) -> str:
        extra_kwargs: dict[str, str] = {}
        if m.startswith("vertex_ai/"):
            extra_kwargs["vertex_location"] = _resolve_vertex_location()
        # Build messages list — prepend system message with cache_control when
        # provided (closed over from enclosing synthesize() scope via Python closure).
        messages: list[dict[str, object]] = []
        if system is not None:
            # LiteLLM (≥1.x) normalises top-level `cache_control` on message
            # dicts into the Anthropic API payload when `content` is a plain
            # str (see litellm/anthropic/chat/transformation.py:
            # translate_system_message). This is a non-standard key placement
            # that works today but is not part of the OpenAI message schema.
            # If a future LiteLLM upgrade silently drops it the prompt cache
            # would stop working but no test would fail (integration tests mock
            # litellm.completion). Pin LiteLLM in pyproject.toml and review
            # this on each major version bump.
            messages.append(
                {
                    "role": "system",
                    "content": system,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        messages.append({"role": "user", "content": p})
        response = litellm.completion(
            model=m,
            messages=messages,
            timeout=t,
            **extra_kwargs,
        )
        return str(response.choices[0].message.content)

    # ------------------------------------------------------------------
    # 5. Call the provider (with bounded retry for transient errors)
    # ------------------------------------------------------------------
    # Auth errors are caught first and re-raised as LLMError immediately —
    # they are permanent failures that must not be retried.
    try:
        return _call_litellm_inner(prompt, resolved_model, timeout)

    except AuthenticationError as exc:
        raise LLMError(
            f"Authentication failed for model '{resolved_model}': {exc}",
            category="auth",
            original=exc,
        ) from exc

    except RateLimitError as exc:
        raise LLMError(
            f"Rate limit exceeded for model '{resolved_model}': {exc}",
            category="rate-limit",
            original=exc,
        ) from exc

    except Exception as exc:
        raise LLMError(
            f"Unexpected error from model '{resolved_model}': {exc}",
            category="general",
            original=exc,
        ) from exc
