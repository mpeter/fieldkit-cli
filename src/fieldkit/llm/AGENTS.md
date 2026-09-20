# AGENTS.md — LLM Wrapper (`llm/`)

Vendor-neutral LLM wrapper backed by LiteLLM. All LLM calls in fieldkit route through this module — no direct SDK calls anywhere else.

## LiteLLM Only — No Direct Anthropic SDK (D032)

**CONSTRAINT:** All LLM calls go through `synthesize()` in `core.py` via LiteLLM → Vertex AI ADC. Do NOT add direct `anthropic` SDK calls anywhere in the codebase.

`ANTHROPIC_API_KEY` is **not used** and **not required**. Do not add code that reads or requires this variable.

## Model Resolution Chain

Resolution order (first non-empty wins):
1. `model` parameter passed to `synthesize()`
2. `FIELDKIT_LLM_MODEL` / `LLM_MODEL` environment variables (must start with `vertex_ai/`)
3. `FIELDKIT_ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` environment variables
   (bare model slug, automatically prefixed with `vertex_ai/` by the resolver)
4. `llm_model` key in `~/.config/fieldkit/config.yaml` (must start with `vertex_ai/`)
5. Hardcoded `_HARDCODED_DEFAULT_MODEL` in `core.py` (`vertex_ai/claude-sonnet-4-6`)

The resolver is `_resolve_model()` in `core.py` — read it for the exact priority logic.
All resolved model strings use the `vertex_ai/` prefix for Vertex AI ADC routing.
Note: `_DEFAULT_MODEL` (no `HARDCODED_`) is a backward-compat alias pointing to the
same constant — always reference `_HARDCODED_DEFAULT_MODEL` in new code and tests.

## Auth

```bash
gcloud auth application-default login
```

The GCP project is set via `GOOGLE_CLOUD_PROJECT` or `VERTEXAI_PROJECT`; no project is selected by default.

Region resolution order (highest priority first):
1. `vertex_location` key in `~/.config/fieldkit/config.yaml`
2. `FIELDKIT_VERTEX_LOCATION` env var (implementation note prefixed alias)
3. `CLOUD_ML_REGION` env var
4. `VERTEX_LOCATION` env var (skips `"global"`)
5. `GOOGLE_CLOUD_REGION` env var
6. Hardcoded default `us-east5`

The resolver is `_resolve_vertex_location()` in `core.py`. `"global"` is skipped at every level (not a valid regional endpoint).

## Two-Layer Model (D036/D037)

**CONSTRAINT:** Every fieldkit tool must follow the two-layer model:

- **Layer 1 (deterministic):** Always runs. No LLM. Must produce valid, useful output on its own. Fully testable in CI without API keys.
- **Layer 2 (LLM insights):** Optional. Appended on top of Layer 1 output. Suppressed by `NO_LLM=1` env var or `--no-llm` flag.

MCP is infrastructure — it is **not** an LLM layer and is **not** disabled by `--no-llm`.

Tests run in Layer 1 mode by default (`NO_LLM=1` in test environment). Layer 1 output must be stable and auditable independently of Layer 2.

## Lazy Import Contract (D035)

**CONSTRAINT:** `import litellm` is done **inside `synthesize()`**, never at module top level.

```python
# WRONG — breaks NO_LLM=1 and adds ~50ms startup cost
import litellm  # at module top level

# CORRECT — lazy import inside synthesize()
def synthesize(...):
    if llm_disabled():  # reads both FIELDKIT_NO_LLM and NO_LLM
        return _NO_LLM_STUB
    import litellm  # only reached when LLM is actually needed
```

Violating this breaks the `NO_LLM=1` contract: the stub path checks the env var before the import statement. If litellm is imported at module level, `NO_LLM=1` still triggers the import cost and may fail in stripped environments where litellm is not installed.

## NO_LLM Stub

When `NO_LLM=1` is set, `synthesize()` returns `_NO_LLM_STUB` (the string `"[LLM STUB] NO_LLM=1 is set — no API call was made."`). Callers that need to detect stub mode should compare against `_NO_LLM_STUB` (exported from `core.py`), not against a hardcoded string.

## Logging

Logging uses stdlib `logging`.

## Retry (implementation note, shared policy)

`synthesize()`, `transcribe()`, and `fetch_gemini_doc()` retry via
`transient_retry()` from `fieldkit.config.retry` — 3 attempts, exponential backoff
2–10s. Do not inline tenacity here; the shared factory carries the per-attempt
`WARNING` and the exhaustion `ERROR`, and omitting them is how eight of the nine
pre-existing retry sites ended up silent.

All three are idempotent reads, which is why they use `transient_retry`. Anything that
creates or mutates server-side state must use `connect_retry` instead — see
`fieldkit.config.retry` for why.

Retried exceptions: `RateLimitError`, `APIConnectionError` (LLM); `HttpError` in
`RETRY_TRANSIENT_STATUSES` = 429/500/502/503/504 (Docs). 502 and 504 were previously
dropped by the Docs predicate. Auth errors (403/404) are **never** retried — they
raise immediately. Do not add retry to callers; it is already in the layer.

## TRANSCRIBE_MODEL

`FIELDKIT_TRANSCRIBE_MODEL` / `TRANSCRIBE_MODEL` env var sets the transcription model
used by `_transcribe.py`. **No default provider is configured** — callers must set this
variable or pass `model=` explicitly.

**Provider constraint (validated 2026-07-10):** LiteLLM's `transcription()` function
does NOT support `vertex_ai` — the vertex_ai provider is unmapped in LiteLLM's
transcription routing and raises `ValueError: "Unmapped provider passed in."` Supported
LiteLLM transcription providers: `azure`, `openai`, `nvidia_riva`, `soniox`, and
OpenAI-compatible providers (groq, deepgram, watsonx). All options except `nvidia_riva`
require external API keys and are therefore disallowed by the provider constraint.

Compliant option (no external API key required):
- `nvidia_riva/<model>` — requires a local NVIDIA Riva gRPC server, plus:
  - `NVIDIA_RIVA_API_BASE=<host:port>` env var (e.g. `localhost:50051`) — mandatory, raises `NvidiaRivaException` if absent
  - `pip install 'litellm[stt-nvidia-riva]'` — riva-client gRPC dependency, not in pyproject.toml

All other transcription options require explicit operator configuration with an approved
provider. Do NOT configure `vertex_ai/*` as a transcription model — it will fail.

## `from __future__ import annotations` Exceptions

Two files intentionally retain `from __future__ import annotations` after the implementation note sweep: `sf/client.py` (forward refs in type stubs) and `commands/ingest/run.py` (threading.Lock at runtime). Do not strip it from these files.

## Error Handling

`LLMError` has a `category` attribute: `"auth"`, `"rate-limit"`, or `"general"`. Auth errors map to exit code 2 (EXIT_AUTH); rate-limit errors map to exit code 1 (EXIT_PARTIAL); general errors map to exit code 3 (EXIT_DATA) via `cli_main()`. Never catch `LLMError` silently in library code.
