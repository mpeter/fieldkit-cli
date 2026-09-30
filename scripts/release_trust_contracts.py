"""Validate structure only; an explicit controller root is not authentication."""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from scripts.json_policy import load_json_bytes
from scripts.rehearsal_acquisition import read_root_bytes

MAX_CONTRACT_BYTES = 256 * 1024
ContractKind = Literal["selection", "receipt"]
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
SCHEMA_NAMES = {"selection": "release-trust-selection", "receipt": "release-controller-receipt"}
SCHEMA_BASE = "https://github.com/mpeter/fieldkit-cli/blob/main/docs/release-readiness/"
UTC_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z")


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid release trust JSON")
    if isinstance(value, dict):
        for child in value.values():
            _reject_nonfinite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_nonfinite(child)


def _object(data: bytes) -> dict[str, JsonValue]:
    if len(data) > MAX_CONTRACT_BYTES:
        raise ValueError("release trust input exceeds byte limit")
    value = load_json_bytes(data)
    _reject_nonfinite(value)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("invalid release trust object")
    return cast(dict[str, JsonValue], value)


def validate_contract(data: bytes, *, kind: ContractKind, controller_root: Path) -> None:
    """Return None for structure, never authority, authentication, or approval.

    Only the two locally acquired schemas resolve. The caller must independently
    authenticate C0 and compare expected identities, signatures, and freshness.
    """
    if kind not in SCHEMA_NAMES:
        raise ValueError("invalid release trust contract kind")
    instance = _object(data)
    formats = FormatChecker()

    def valid_datetime(value: object) -> bool:
        if not isinstance(value, str):
            return True
        return UTC_DATETIME.fullmatch(value) is not None and datetime.fromisoformat(value).tzinfo is not None

    formats.checks("date-time", raises=ValueError)(valid_datetime)

    schemas: dict[str, dict[str, JsonValue]] = {}
    try:
        for name in SCHEMA_NAMES.values():
            schema = _object(read_root_bytes(controller_root, f"docs/release-readiness/{name}.schema.json"))
            if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                raise ValueError("invalid release trust schema dialect")
            if schema.get("$id") != f"{SCHEMA_BASE}{name}.schema.json":
                raise ValueError("invalid release trust schema identity")
            Draft202012Validator.check_schema(schema)
            schemas[name] = schema
        registry: Registry = Registry().with_resources(
            (f"{SCHEMA_BASE}{name}.schema.json", Resource.from_contents(schema)) for name, schema in schemas.items()
        )
        validator = Draft202012Validator(schemas[SCHEMA_NAMES[kind]], registry=registry, format_checker=formats)
        if next(validator.iter_errors(instance), None) is not None:
            raise ValueError("release trust contract structure is invalid")
    except (SchemaError, Unresolvable):
        raise ValueError("release trust schema is invalid or unresolved") from None
