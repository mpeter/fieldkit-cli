"""Bounded JSON object and schema checks shared by release evidence validators."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from scripts.json_policy import load_json_bytes, require_bounded_json_depth
from scripts.rehearsal_evidence import MAX_EVIDENCE_BYTES


def load_object(raw: bytes, subject: str) -> dict[str, Any]:
    """Decode one bounded JSON object from already acquired bytes."""
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"{subject} exceeds the {MAX_EVIDENCE_BYTES}-byte limit")
    try:
        value = load_json_bytes(raw)
    except ValueError:
        raise ValueError(f"{subject} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be a JSON object")
    return value


def schema_valid(schema: dict[str, Any], value: Any) -> bool:
    require_bounded_json_depth(value)
    try:
        return not any(Draft202012Validator(schema).iter_errors(value))
    except RecursionError:
        raise ValueError("release evidence schema validation exceeds nesting limit") from None
