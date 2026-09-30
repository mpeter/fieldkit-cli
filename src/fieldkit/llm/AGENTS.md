# fieldkit LLM contributor guide

Keep synthesis and transcription behind their existing domain wrappers so
callers share provider routing, optional-dependency handling, and retry policy.

## Synthesis routing

`fieldkit.llm.core.synthesize()` uses LiteLLM with Vertex AI Application Default
Credentials. Its model resolver rejects names without the `vertex_ai/` prefix;
this is not a provider-neutral synthesis interface. Direct Anthropic API keys
are not used by this path.

`_resolve_model()` owns model precedence and validation. `_resolve_vertex_location()`
owns region selection. Reuse these implementations rather than reproducing
their environment/configuration logic in callers. The public environment
reference documents the supported settings.

## Offline and optional behavior

`fieldkit.config.llm_disabled()` checks only `FIELDKIT_NO_LLM`, the canonical
no-LLM environment variable.
Any non-empty value disables calls, including the string `0`. Both wrappers
return their own deterministic stub before importing LiteLLM or attempting a
provider request. Preserve that ordering so a base installation works without
optional LLM dependencies.

Consumers need useful deterministic behavior without an LLM. Do not render a
stub as generated insight or require credentials to test that path. A no-LLM
setting disables model calls; it does not disable unrelated integration traffic.

## Transcription is a separate route

Transcription selects an explicit `model=` argument, then
`FIELDKIT_TRANSCRIBE_MODEL`. There is no default
transcription provider. Unlike synthesis, this wrapper does not enforce the
Vertex-only model prefix: it passes the selected model to LiteLLM transcription.

Do not infer transcription support from synthesis support. Provider availability,
credentials, and optional provider dependencies must be verified for the chosen
transcription route before sending audio. Do not describe an operator preference
as an enforced provider allowlist.

## Bounds, retries, and failures

Synthesis bounds prompt length before provider import. Keep static system
instructions separate from external text, and preserve input guards in callers.

Both wrappers pass request timeouts to LiteLLM and use the shared
`fieldkit.config.retry.transient_retry()` policy for selected transient errors.
These are per-request timeout settings, not proof of a provider-independent
whole-operation deadline. Do not add a second retry loop in callers or retry
authentication failures as transient errors.

Preserve categorized `LLMError` failures for the top-level CLI handler:
authentication maps to exit 2, rate limiting to exit 1, and other categories to
exit 3. Transcription raises its distinct `TranscribeError`; do not assume it
has the same category-to-exit mapping. Tests of CLI behavior must exercise the
actual top-level handler, not only exception attributes.
