"""Validate retained frontier review execution and its same-candidate result."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from scripts import release_evidence_json

_APPROVED_FRONTIER_MODELS = frozenset({"claude-opus-5-5"})
_FRONTIER_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "review_mode",
        "scope",
        "reviewed_revision",
        "decision",
        "findings",
        "unresolved_findings",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "review_mode": {"const": "fresh-eyes"},
        "scope": {"const": "final-release-candidate"},
        "reviewed_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "decision": {"const": "pass"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "severity", "status", "summary"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "severity": {"enum": ["critical", "high", "medium", "low"]},
                    "status": {"enum": ["resolved", "not-applicable"]},
                    "summary": {"type": "string", "minLength": 1},
                },
            },
        },
        "unresolved_findings": {"const": []},
    },
}
_FRONTIER_SCHEMA_JSON = json.dumps(_FRONTIER_RESULT_SCHEMA, sort_keys=True, separators=(",", ":"))
CLI_ARGV = (
    "claude",
    "--print",
    "--safe-mode",
    "--restricted",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
    "--tools",
    "Read,Glob,Grep",
    "--allowedTools",
    "Read,Glob,Grep",
    "--permission-mode",
    "plan",
    "--permission-prompts",
    "none",
    "--model",
    "opus",
    "--effort",
    "high",
    "--output-format",
    "json",
    "--json-schema",
    _FRONTIER_SCHEMA_JSON,
    "--no-session-persistence",
)
PROMPT_TERMS = (
    "architecture",
    "python",
    "cli adapters",
    "configuration",
    "error/exit handling",
    "persistence",
    "integrations",
    "tests",
    "ci",
    "documentation",
    "agent instructions",
    "references",
)


def valid_proof(
    prompt: bytes | None,
    response: bytes | None,
    version_bytes: bytes | None,
    payload: Mapping[str, object],
    candidate: Mapping[str, object],
) -> bool:
    """Bind frontier proof to retained prompt, version, exact argv, and review bytes."""
    try:
        prompt_argument = prompt.decode("utf-8") if prompt is not None else None
        retained_version = version_bytes.decode("utf-8").strip() if version_bytes is not None else None
    except UnicodeDecodeError:
        return False
    return (
        prompt is not None
        and response is not None
        and version_bytes is not None
        and payload["reviewer_role"] == "independent-model"
        and payload["review_mode"] == "fresh-eyes"
        and payload["scope"] == "final-release-candidate"
        and isinstance(payload["cli_version"], str)
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)", payload["cli_version"]) is not None
        and retained_version == payload["cli_version"]
        and payload["cli_argv"] == [*CLI_ARGV, prompt_argument]
        and valid_review(prompt, response, payload, candidate)
    )


def valid_review(
    raw_prompt: bytes,
    raw_response: bytes,
    payload: Mapping[str, object],
    candidate: Mapping[str, object],
) -> bool:
    try:
        prompt = raw_prompt.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lowered = prompt.lower()
    if (
        str(candidate["source_sha"]) not in prompt
        or "final release candidate" not in lowered
        or "fresh-eyes" not in lowered
        or any(term not in lowered for term in PROMPT_TERMS)
    ):
        return False
    response = release_evidence_json.load_object(raw_response, "frontier CLI response")
    usage = response.get("modelUsage")
    model = payload.get("model")
    if (
        response.get("type") != "result"
        or response.get("subtype") != "success"
        or response.get("is_error") is not False
        or not isinstance(response.get("result"), str)
        or not response["result"]
        or not isinstance(model, str)
        or model not in _APPROVED_FRONTIER_MODELS
        or not isinstance(usage, dict)
        or set(usage) != {model}
        or not isinstance(usage[model], dict)
        or usage[model].get("canonicalModel") != model
    ):
        return False
    structured = response.get("structured_output")
    if not release_evidence_json.schema_valid(_FRONTIER_RESULT_SCHEMA, structured):
        return False
    return (
        isinstance(structured, dict)
        and structured.get("reviewed_revision") == candidate["source_sha"] == payload["reviewed_revision"]
        and structured.get("findings") == payload["findings"]
        and structured.get("unresolved_findings") == payload["unresolved_findings"] == []
    )
