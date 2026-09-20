"""Environment helpers for tests that exercise env-var resolution.

A test that patches ``os.environ`` to prove "setting X selects Y" is only valid if the
*other* inputs to that resolution are absent. Several tests built their base environment
by snapshotting the ambient one and scrubbing a couple of keys by hand, which meant
``patch.dict(..., clear=True)`` cleared the real environment and then re-injected the
operator's settings from the snapshot. The test looked hermetic and was not: it passed
on CI, where no fieldkit vars are set, and failed on any machine with a working config
(historic regression).

``hermetic_env()`` keeps the ambient environment — credentials and Vertex project
settings are needed for the code under test to import and run — while removing every
variable that participates in model or endpoint *selection*. Each test then declares
exactly the keys it is exercising.
"""

import os

SELECTION_ENV_KEYS = frozenset(
    {
        # LLM on/off
        "NO_LLM",
        "FIELDKIT_NO_LLM",
        # model selection — each has a FIELDKIT_-prefixed alias that wins over the bare
        # name (implementation note), which is precisely what makes a half-scrubbed environment
        # produce a passing-but-meaningless test
        "LLM_MODEL",
        "FIELDKIT_LLM_MODEL",
        "TRANSCRIBE_MODEL",
        "FIELDKIT_TRANSCRIBE_MODEL",
        "ANTHROPIC_MODEL",
        "FIELDKIT_ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        # Vertex region selection — the priority chain is config.yaml, then
        # FIELDKIT_VERTEX_LOCATION, CLOUD_ML_REGION, VERTEX_LOCATION,
        # GOOGLE_CLOUD_REGION, so a leaked lower-priority value can still decide
        # the outcome when the test only sets the higher ones.
        "FIELDKIT_VERTEX_LOCATION",
        "VERTEX_LOCATION",
        "CLOUD_ML_REGION",
        "GOOGLE_CLOUD_REGION",
        # endpoint selection
        "FIELDKIT_MCP_GATEWAY_URL",
    }
)
"""Every env var that selects a model or endpoint, prefixed alias included.

Deliberately excludes credentials (``ANTHROPIC_VERTEX_PROJECT_ID`` and friends): the
code under test needs them to import, and they do not steer resolution.
"""


def hermetic_env(**overrides: str) -> dict[str, str]:
    """Return the ambient environment with all selection keys removed, plus overrides.

    Use with ``patch.dict(os.environ, hermetic_env(LLM_MODEL="x"), clear=True)`` so the
    only selection variable present is the one under test.
    """
    base = {k: v for k, v in os.environ.items() if k not in SELECTION_ENV_KEYS}
    return {**base, **overrides}
