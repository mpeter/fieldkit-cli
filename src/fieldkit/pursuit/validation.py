"""Fail-closed validation for pursuit frontmatter documents."""

from __future__ import annotations

import importlib.resources
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError

from fieldkit.errors import FieldkitError
from fieldkit.pursuit.io import split_frontmatter_raw
from fieldkit.util.strict_yaml import StrictYAMLError, load_strict_yaml

_ADJACENT_FRONTMATTER_RE = re.compile(r"\A\n?---(?:\r?\n|\Z)")


@dataclass(frozen=True)
class PursuitQualityResult:
    """Local quality observations from one parsed pursuit document."""

    has_frontmatter: bool
    backstory_paths: tuple[str, ...]


@dataclass(frozen=True)
class PursuitDocument:
    """One parsed frontmatter mapping and its byte-preserved document body."""

    frontmatter: dict[str, Any]
    body: str


def pursuit_schema_path() -> Path:
    """Return the installed pursuit schema path."""
    resource = importlib.resources.files("fieldkit._data").joinpath("pursuit-frontmatter.schema.json")
    return Path(str(resource))


def _json_compatible(value: Any) -> Any:
    """Convert YAML date values into the JSON strings expected by the schema."""
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def parse_pursuit_content(content: str) -> PursuitDocument | None:
    """Parse one anchored frontmatter mapping and reject ambiguous input."""
    split = split_frontmatter_raw(content)
    if split is None:
        return None
    frontmatter_text, body = split
    if _ADJACENT_FRONTMATTER_RE.match(body):
        raise FieldkitError("Pursuit contains multiple frontmatter blocks")
    try:
        loaded = load_strict_yaml(frontmatter_text)
    except StrictYAMLError as exc:
        if exc.reason == "duplicate_key":
            raise FieldkitError("Duplicate pursuit frontmatter mapping keys") from None
        raise FieldkitError("Invalid pursuit frontmatter") from None
    if loaded is None:
        return PursuitDocument(frontmatter={}, body=body)
    if not isinstance(loaded, dict):
        raise FieldkitError("Invalid pursuit frontmatter")
    frontmatter: dict[str, Any] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise FieldkitError("Invalid pursuit frontmatter")
        frontmatter[key] = value
    return PursuitDocument(frontmatter=frontmatter, body=body)


def validate_pursuit_content(content: str, *, schema_path: Path) -> tuple[str, ...]:
    """Return stable schema errors without reflecting frontmatter contents."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError):
        return ("Pursuit schema unavailable or invalid",)

    try:
        parsed = parse_pursuit_content(content)
    except FieldkitError as exc:
        return (str(exc),)
    if parsed is None:
        return ("Missing or incomplete pursuit frontmatter",)
    frontmatter = parsed.frontmatter
    if any(key.startswith("sf-") for key in frontmatter):
        return ("Hyphenated Salesforce frontmatter keys are invalid; use underscores",)
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for error in validator.iter_errors(_json_compatible(frontmatter)):
        keyword = (
            error.validator
            if isinstance(error.validator, str) and len(error.validator) <= 32 and error.validator.isidentifier()
            else "schema"
        )
        errors.append(f"Pursuit frontmatter schema violation ({keyword})")
    return tuple(errors)


def inspect_pursuit_quality(content: str) -> PursuitQualityResult:
    """Return Backstory-reference locations or raise on unreadable frontmatter."""
    parsed = parse_pursuit_content(content)
    if parsed is None:
        return PursuitQualityResult(has_frontmatter=False, backstory_paths=())
    paths: list[str] = []
    _collect_backstory_paths(parsed.frontmatter, path="", paths=paths)
    return PursuitQualityResult(has_frontmatter=True, backstory_paths=tuple(paths))


def _collect_backstory_paths(value: object, *, path: str, paths: list[str]) -> None:
    """Append paths containing a Backstory marker in deterministic order."""
    if isinstance(value, str):
        if "[backstory" in value.lower():
            paths.append(path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = str(key) if not path else f"{path}.{key}"
            _collect_backstory_paths(item, path=child, paths=paths)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_backstory_paths(item, path=f"{path}[{index}]", paths=paths)
