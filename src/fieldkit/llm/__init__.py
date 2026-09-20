"""fieldkit.llm — LLM inference, logging, and transcription."""

from fieldkit.llm._transcribe import (
    TranscribeError as TranscribeError,
)
from fieldkit.llm._transcribe import (
    transcribe as transcribe,
)
from fieldkit.llm.core import (
    _DEFAULT_MODEL as _DEFAULT_MODEL,
)
from fieldkit.llm.core import (
    _NO_LLM_STUB as _NO_LLM_STUB,
)
from fieldkit.llm.core import (
    LLM_SYNTHESIS_TIMEOUT as LLM_SYNTHESIS_TIMEOUT,
)
from fieldkit.llm.core import (
    _resolve_model as _resolve_model,
)
from fieldkit.llm.core import (
    synthesize as synthesize,
)
from fieldkit.llm.log import (
    LLM_ACCOUNT as LLM_ACCOUNT,
)
from fieldkit.llm.log import (
    LLM_SKILL as LLM_SKILL,
)
from fieldkit.llm.log import (
    init_db as init_db,
)
from fieldkit.llm.log import (
    set_skill_context as set_skill_context,
)
from fieldkit.llm.sanitize import (
    UNTRUSTED_DATA_PREAMBLE as UNTRUSTED_DATA_PREAMBLE,
)
from fieldkit.llm.sanitize import (
    wrap_user_data as wrap_user_data,
)

__all__ = [
    "LLM_ACCOUNT",
    "LLM_SKILL",
    "LLM_SYNTHESIS_TIMEOUT",
    "UNTRUSTED_DATA_PREAMBLE",
    "_DEFAULT_MODEL",
    "_NO_LLM_STUB",
    "TranscribeError",
    "_resolve_model",
    "init_db",
    "set_skill_context",
    "synthesize",
    "transcribe",
    "wrap_user_data",
]
