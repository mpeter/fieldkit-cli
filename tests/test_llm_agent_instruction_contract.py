"""Local wrapper proof for the LLM contributor instruction surface."""

import builtins
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

import fieldkit.config.retry as retry_policy
import fieldkit.llm._transcribe as transcription
import fieldkit.llm.core as synthesis
import fieldkit.llm.log as llm_log
from fieldkit.errors import LLMError

pytestmark = pytest.mark.unit

_CLAIMS = (
    "Keep synthesis and transcription behind their existing domain wrappers so callers share provider routing, optional-dependency handling, and retry policy.",
    "`fieldkit.llm.core.synthesize()` uses LiteLLM with Vertex AI Application Default Credentials. Its model resolver rejects names without the `vertex_ai/` prefix; this is not a provider-neutral synthesis interface. Direct Anthropic API keys are not used by this path.",
    "`_resolve_model()` owns model precedence and validation. `_resolve_vertex_location()` owns region selection. Reuse these implementations rather than reproducing their environment/configuration logic in callers. The public environment reference documents the supported settings.",
    "`fieldkit.config.llm_disabled()` checks only `FIELDKIT_NO_LLM`, the canonical no-LLM environment variable. Any non-empty value disables calls, including the string `0`. Both wrappers return their own deterministic stub before importing LiteLLM or attempting a provider request. Preserve that ordering so a base installation works without optional LLM dependencies.",
    "Consumers need useful deterministic behavior without an LLM. Do not render a stub as generated insight or require credentials to test that path. A no-LLM setting disables model calls; it does not disable unrelated integration traffic.",
    "Transcription selects an explicit `model=` argument, then `FIELDKIT_TRANSCRIBE_MODEL`. There is no default transcription provider. Unlike synthesis, this wrapper does not enforce the Vertex-only model prefix: it passes the selected model to LiteLLM transcription.",
    "Do not infer transcription support from synthesis support. Provider availability, credentials, and optional provider dependencies must be verified for the chosen transcription route before sending audio. Do not describe an operator preference as an enforced provider allowlist.",
    "Synthesis bounds prompt length before provider import. Keep static system instructions separate from external text, and preserve input guards in callers.",
    "Both wrappers pass request timeouts to LiteLLM and use the shared `fieldkit.config.retry.transient_retry()` policy for selected transient errors. These are per-request timeout settings, not proof of a provider-independent whole-operation deadline. Do not add a second retry loop in callers or retry authentication failures as transient errors.",
    "Preserve categorized `LLMError` failures for the top-level CLI handler: authentication maps to exit 2, rate limiting to exit 1, and other categories to exit 3. Transcription raises its distinct `TranscribeError`; do not assume it has the same category-to-exit mapping. Tests of CLI behavior must exercise the actual top-level handler, not only exception attributes.",
)


def _instruction_document() -> str:
    return (Path(__file__).parents[1] / "src/fieldkit/llm/AGENTS.md").read_text(encoding="utf-8")


def _assert_instruction_claims(document: str) -> None:
    paragraphs = tuple(
        " ".join(paragraph.split()) for paragraph in re.split(r"\n[ \t]*\n", document) if paragraph.strip()
    )
    expected = (
        "# fieldkit LLM contributor guide",
        _CLAIMS[0],
        "## Synthesis routing",
        _CLAIMS[1],
        _CLAIMS[2],
        "## Offline and optional behavior",
        _CLAIMS[3],
        _CLAIMS[4],
        "## Transcription is a separate route",
        _CLAIMS[5],
        _CLAIMS[6],
        "## Bounds, retries, and failures",
        _CLAIMS[7],
        _CLAIMS[8],
        _CLAIMS[9],
    )
    assert paragraphs == expected, "unreviewed LLM instruction scope"


def test_instruction_claims_preserve_reviewed_wrapper_scope() -> None:
    _assert_instruction_claims(_instruction_document())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("rejects names without", "accepts names without"),
        ("not a provider-neutral", "a provider-neutral"),
        ("Direct Anthropic API keys are not used", "Direct Anthropic API keys are used"),
        ("checks only `FIELDKIT_NO_LLM`", "checks `NO_LLM` instead"),
        ("including the string `0`", "excluding the string `0`"),
        ("stub before importing", "stub after importing"),
        ("does not disable unrelated integration traffic", "disables unrelated integration traffic"),
        (
            "an explicit `model=` argument, then `FIELDKIT_TRANSCRIBE_MODEL`",
            "`FIELDKIT_TRANSCRIBE_MODEL`, then an explicit `model=` argument",
        ),
        ("no default transcription provider", "a default transcription provider"),
        ("does not enforce the Vertex-only model prefix", "enforces the Vertex-only model prefix"),
        ("Do not infer transcription support", "Infer transcription support"),
        ("before provider import", "after provider import"),
        (
            "not proof of a provider-independent whole-operation deadline",
            "proof of a provider-independent whole-operation deadline",
        ),
        ("or retry authentication failures", "but retry authentication failures"),
        ("authentication maps to exit 2", "authentication maps to exit 1"),
        ("do not assume it has the same", "assume it has the same"),
        ("not only exception attributes", "only exception attributes"),
        ("Keep synthesis and transcription", "It is false that Keep synthesis and transcription"),
    ],
)
def test_instruction_rejects_false_wrapper_scope(before: str, after: str) -> None:
    document = "\n\n".join(" ".join(paragraph.split()) for paragraph in _instruction_document().split("\n\n"))
    assert before in document
    with pytest.raises(AssertionError, match="LLM instruction scope"):
        _assert_instruction_claims(document.replace(before, after))


@pytest.mark.parametrize(
    "addition",
    [
        "Authentication failures may be retried.",
        "## Additional retry policy\n\nAuthentication failures may be retried.",
        "# Authentication failures may be retried.",
    ],
)
def test_instruction_rejects_appended_unreviewed_instructions(addition: str) -> None:
    document = _instruction_document()
    with pytest.raises(AssertionError, match="LLM instruction scope"):
        _assert_instruction_claims(document + "\n\n" + addition)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "# fieldkit LLM contributor guide",
            "# Authentication failures may be retried. fieldkit LLM contributor guide",
        ),
        ("## Synthesis routing", "## Synthesis routing — authentication failures may be retried."),
        ("## Synthesis routing", "## Transcription is a separate route"),
    ],
)
def test_instruction_rejects_unreviewed_heading_instructions(before: str, after: str) -> None:
    document = _instruction_document()
    assert before in document
    with pytest.raises(AssertionError, match="LLM instruction scope"):
        _assert_instruction_claims(document.replace(before, after, 1))


def test_instruction_rejects_reordered_reviewed_paragraphs() -> None:
    document = "\n\n".join(" ".join(paragraph.split()) for paragraph in _instruction_document().split("\n\n"))
    before = _CLAIMS[1] + "\n\n" + _CLAIMS[2]
    after = _CLAIMS[2] + "\n\n" + _CLAIMS[1]
    assert before in document
    with pytest.raises(AssertionError, match="LLM instruction scope"):
        _assert_instruction_claims(document.replace(before, after, 1))


class _AuthenticationError(Exception):
    pass


class _RateLimitError(Exception):
    pass


class _APIConnectionError(Exception):
    pass


@dataclass
class _ProviderBoundary:
    completion: Mock
    transcription: Mock
    imports: list[str]


@pytest.fixture
def provider_boundary(monkeypatch: pytest.MonkeyPatch) -> _ProviderBoundary:
    """Install only provider boundary doubles; retain wrapper, routing and retry code."""
    boundary = _ProviderBoundary(
        Mock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="fictional synthesis"))]
            )
        ),
        Mock(return_value=SimpleNamespace(text="fictional transcript")),
        [],
    )
    exceptions = ModuleType("litellm.exceptions")
    exceptions.__dict__.update(AuthenticationError=_AuthenticationError, RateLimitError=_RateLimitError)
    litellm = ModuleType("litellm")
    litellm.__dict__.update(exceptions=exceptions, completion=boundary.completion, transcription=boundary.transcription)
    openai = ModuleType("openai")
    openai.__dict__["APIConnectionError"] = _APIConnectionError
    for name, module in (
        ("litellm", litellm),
        ("litellm.exceptions", exceptions),
        ("openai", openai),
        ("vertexai", ModuleType("vertexai")),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    original_import = builtins.__import__

    def tracked_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name.split(".", 1)[0] in {"litellm", "openai", "vertexai"}:
            boundary.imports.append(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracked_import)
    monkeypatch.setattr(llm_log, "_ensure_initialized", Mock())
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv("FIELDKIT_TRANSCRIBE_MODEL", "test-provider/fictional-audio")
    return boundary


def test_documented_prompt_bound_precedes_provider_import(provider_boundary: _ProviderBoundary) -> None:
    _assert_instruction_claims(_instruction_document())

    with pytest.raises(LLMError, match="Prompt exceeds maximum size") as error:
        synthesis.synthesize("x" * (synthesis._MAX_PROMPT_CHARS + 1), model="vertex_ai/fictional-model")

    assert error.value.category == "general"
    assert provider_boundary.imports == []
    provider_boundary.completion.assert_not_called()


@pytest.mark.parametrize("timeout", [17, 120])
def test_documented_synthesis_forwards_request_timeout(provider_boundary: _ProviderBoundary, timeout: int) -> None:
    _assert_instruction_claims(_instruction_document())

    result = synthesis.synthesize("Fictional external context", model="vertex_ai/fictional-model", timeout=timeout)

    assert result == "fictional synthesis"
    provider_boundary.completion.assert_called_once()
    assert provider_boundary.completion.call_args.kwargs["timeout"] == timeout
    assert "litellm" in provider_boundary.imports
    assert "openai" in provider_boundary.imports


@pytest.mark.parametrize("exception_type", [_RateLimitError, _APIConnectionError])
@pytest.mark.parametrize("succeeds", [False, True])
def test_documented_transcription_uses_shared_transient_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_boundary: _ProviderBoundary,
    exception_type: type[Exception],
    succeeds: bool,
) -> None:
    _assert_instruction_claims(_instruction_document())
    audio = tmp_path / "fictional.wav"
    audio.write_bytes(b"fictional audio")
    failure = exception_type("synthetic provider failure")
    provider_boundary.transcription.side_effect = [
        failure,
        failure,
        SimpleNamespace(text=" recovered transcript ") if succeeds else failure,
    ]
    monkeypatch.setattr(transcription, "transient_retry", partial(retry_policy.transient_retry, wait_min=0, wait_max=0))

    if succeeds:
        result = transcription.transcribe(audio)
        assert result == "recovered transcript"
    else:
        with pytest.raises(transcription.TranscribeError, match="synthetic provider failure") as error:
            transcription.transcribe(audio)
        assert error.value.original is failure
        assert error.value.category == ("rate-limit" if exception_type is _RateLimitError else "general")
        assert any("failed after 3 attempts" in note for note in getattr(failure, "__notes__", ()))
    assert provider_boundary.transcription.call_count == 3
    assert all(call.kwargs["file"].closed for call in provider_boundary.transcription.call_args_list)
    provider_boundary.completion.assert_not_called()


def test_documented_transcription_authentication_is_not_retried(
    tmp_path: Path, provider_boundary: _ProviderBoundary
) -> None:
    _assert_instruction_claims(_instruction_document())
    audio = tmp_path / "fictional.wav"
    audio.write_bytes(b"fictional audio")
    failure = _AuthenticationError("synthetic authentication failure")
    provider_boundary.transcription.side_effect = failure

    with pytest.raises(transcription.TranscribeError, match="Authentication failed") as error:
        transcription.transcribe(audio)

    assert error.value.category == "auth"
    assert error.value.original is failure
    provider_boundary.transcription.assert_called_once()
    assert provider_boundary.transcription.call_args.kwargs["file"].closed
    provider_boundary.completion.assert_not_called()
