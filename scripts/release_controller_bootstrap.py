"""Bind C0 bytes to an externally supplied pin; never approve a release.

Load from an independently reviewed deployment with an explicit scripts package
and closed interpreter import roots. Neither these dataclasses nor matching
hashes establish that the caller's deployment state was independently approved.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from scripts.json_policy import load_json_bytes
from scripts.release_controller_closure import (
    MAX_CONTROLLER_BYTES,
    MAX_CONTROLLER_MEMBERS,
    ControllerCapture,
    ControllerMember,
    capture_controller_closure,
    validate_controller_capture,
)

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
MAX_BOOTSTRAP_JSON_BYTES = 256 * 1024
_SCHEMA_BASE = "https://github.com/mpeter/fieldkit-cli/blob/main/docs/release-readiness/"
_UTC_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z")


@dataclass(frozen=True)
class ExternalSelectionAnchor:
    """Deployment-supplied pin metadata, not proof of independent approval."""

    selection_sha256: str
    approval_reference: str
    valid_after: datetime
    valid_before: datetime
    generation: int


@dataclass(frozen=True)
class BootstrapDeploymentPolicy:
    """Immutable schema copies and generations supplied by the trusted host."""

    selection_schema_bytes: bytes
    receipt_schema_bytes: bytes
    allowed_generations: frozenset[int]


@dataclass(frozen=True)
class SelectionBoundControllerCapture:
    """Pin/C0 agreement only; no runtime, signature, API or release authority."""

    selection_bytes: bytes
    selection_sha256: str
    controller_manifest_bytes: bytes
    controller_manifest_sha256: str
    anchor_reference: str
    anchor_generation: int
    controller: ControllerCapture


def _bounded_bytes(data: bytes) -> None:
    if type(data) is not bytes or not 0 < len(data) <= MAX_BOOTSTRAP_JSON_BYTES:
        raise ValueError("bootstrap input must be bounded immutable bytes")


def _object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("bootstrap JSON must contain an object")
    return cast(dict[str, JsonValue], value)


def _parse(data: bytes) -> dict[str, JsonValue]:
    _bounded_bytes(data)
    value = load_json_bytes(data)
    pending = [value]
    while pending:
        child = pending.pop()
        if isinstance(child, float) and not math.isfinite(child):
            raise ValueError("bootstrap JSON contains nonfinite numbers")
        if isinstance(child, dict):
            pending.extend(child.values())
        elif isinstance(child, list):
            pending.extend(child)
    return _object(value)


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("bootstrap digest is invalid")
    return value


def _validate_anchor(anchor: ExternalSelectionAnchor, deployment: BootstrapDeploymentPolicy, now: datetime) -> None:
    if not isinstance(anchor, ExternalSelectionAnchor) or not isinstance(deployment, BootstrapDeploymentPolicy):
        raise ValueError("external anchor and deployment policy are required")
    _digest(anchor.selection_sha256)
    if (
        not isinstance(anchor.approval_reference, str)
        or re.fullmatch(r"[\x21-\x7e]{1,256}", anchor.approval_reference) is None
    ):
        raise ValueError("external anchor reference is invalid")
    if type(deployment.allowed_generations) is not frozenset or any(
        type(generation) is not int or generation <= 0 for generation in deployment.allowed_generations
    ):
        raise ValueError("deployment generations are invalid")
    if type(anchor.generation) is not int or anchor.generation not in deployment.allowed_generations:
        raise ValueError("external anchor generation is disallowed")
    if any(
        not isinstance(value, datetime) or value.utcoffset() is None
        for value in (now, anchor.valid_after, anchor.valid_before)
    ):
        raise ValueError("external anchor times must be timezone aware")
    if not anchor.valid_after <= now < anchor.valid_before:
        raise ValueError("external anchor is outside its validity interval")


def _closed_schema_references(schema: dict[str, JsonValue], names: set[str]) -> None:
    allowed = {prefix + name + ".schema.json" for name in names for prefix in ("", _SCHEMA_BASE)}
    pending: list[JsonValue] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for keyword in ("$ref", "$dynamicRef"):
                if keyword not in value:
                    continue
                reference = value[keyword]
                if not isinstance(reference, str) or not (
                    reference.startswith("#") or reference.split("#", 1)[0] in allowed
                ):
                    raise ValueError("deployed schema contains an unregistered reference")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def _validate_selection(data: bytes, deployment: BootstrapDeploymentPolicy) -> dict[str, JsonValue]:
    instance = _parse(data)
    schemas = {
        name: _parse(raw)
        for name, raw in (
            ("release-trust-selection", deployment.selection_schema_bytes),
            ("release-controller-receipt", deployment.receipt_schema_bytes),
        )
    }
    formats = FormatChecker()

    def valid_datetime(value: object) -> bool:
        return not isinstance(value, str) or (
            _UTC_DATETIME.fullmatch(value) is not None and datetime.fromisoformat(value).tzinfo is not None
        )

    formats.checks("date-time", raises=ValueError)(valid_datetime)
    try:
        for name, schema in schemas.items():
            if (
                schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
                or schema.get("$id") != f"{_SCHEMA_BASE}{name}.schema.json"
            ):
                raise ValueError("deployed schema identity or dialect is invalid")
            _closed_schema_references(schema, set(schemas))
            Draft202012Validator.check_schema(schema)
        registry: Registry = Registry().with_resources(
            (f"{_SCHEMA_BASE}{name}.schema.json", Resource.from_contents(schema)) for name, schema in schemas.items()
        )
        validator = Draft202012Validator(schemas["release-trust-selection"], registry=registry, format_checker=formats)
        if next(validator.iter_errors(instance), None) is not None:
            raise ValueError("selection structure is invalid")
    except (SchemaError, Unresolvable):
        raise ValueError("deployed schema is invalid or unresolved") from None
    policy = _object(instance["policy"])
    if (
        policy["selection_schema_sha256"] != hashlib.sha256(deployment.selection_schema_bytes).hexdigest()
        or policy["receipt_schema_sha256"] != hashlib.sha256(deployment.receipt_schema_bytes).hexdigest()
    ):
        raise ValueError("selection schema digests disagree with deployment")
    return instance


def _manifest(data: bytes) -> tuple[str, tuple[ControllerMember, ...]]:
    manifest = _parse(data)
    if (
        set(manifest) != {"schema_version", "kind", "entrypoint", "members"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != "fieldkit.release-controller-manifest"
    ):
        raise ValueError("detached controller manifest structure is invalid")
    entrypoint = manifest["entrypoint"]
    rows = manifest["members"]
    if not isinstance(entrypoint, str) or not isinstance(rows, list) or not 0 < len(rows) <= MAX_CONTROLLER_MEMBERS:
        raise ValueError("detached controller manifest entrypoint or member count is invalid")
    members: list[ControllerMember] = []
    for row in rows:
        member = _object(row)
        if set(member) != {"path", "size_bytes", "sha256"}:
            raise ValueError("detached controller manifest member fields are invalid")
        path, size = member["path"], member["size_bytes"]
        if not isinstance(path, str) or type(size) is not int or not 0 <= size <= MAX_CONTROLLER_BYTES:
            raise ValueError("detached controller manifest member metadata is invalid")
        members.append(ControllerMember(path, size, _digest(member["sha256"])))
    paths = [member.path for member in members]
    if paths != sorted(set(paths)) or sum(member.size for member in members) > MAX_CONTROLLER_BYTES:
        raise ValueError("detached controller manifest members must be sorted, unique and bounded")
    return entrypoint, tuple(members)


def _initial_export(capture: ControllerCapture) -> None:
    entrypoint = PurePosixPath(capture.entrypoint)
    paths = {path for path, _ in capture.members}
    if entrypoint.suffix != ".py" or entrypoint.parent == PurePosixPath("."):
        raise ValueError("initial-export entrypoint must belong to an explicit Python package")
    if any(str(parent / "__init__.py") not in paths for parent in entrypoint.parents if parent != PurePosixPath(".")):
        raise ValueError("initial-export controller package initializer is missing")
    for path, raw in capture.members:
        if path in {"fieldkit", "fieldkit.py", "src/fieldkit.py"} or path.startswith(("fieldkit/", "src/fieldkit/")):
            raise ValueError("application members are forbidden in initial-export C0")
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError, RecursionError):
            raise ValueError("initial-export controller Python syntax is invalid") from None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "src"
                and any(alias.name == "fieldkit" for alias in node.names)
            ):
                raise ValueError("application imports are forbidden in initial-export C0")
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(
                name == "fieldkit"
                or name.startswith("fieldkit.")
                or name == "src.fieldkit"
                or name.startswith("src.fieldkit.")
                for name in names
            ):
                raise ValueError("application imports are forbidden in initial-export C0")


@dataclass(frozen=True)
class _SelectionManifest:
    selection: dict[str, JsonValue]
    selection_sha256: str
    manifest_sha256: str
    entrypoint: str
    members: tuple[ControllerMember, ...]


def _selection_manifest(
    selection_bytes: bytes,
    controller_manifest_bytes: bytes,
    *,
    anchor: ExternalSelectionAnchor,
    deployment: BootstrapDeploymentPolicy,
    now: datetime,
) -> _SelectionManifest:
    _bounded_bytes(selection_bytes)
    _validate_anchor(anchor, deployment, now)
    selection_sha256 = hashlib.sha256(selection_bytes).hexdigest()
    if not hmac.compare_digest(selection_sha256, anchor.selection_sha256):
        raise ValueError("raw selection digest disagrees with external anchor")
    selection = _validate_selection(selection_bytes, deployment)
    controller = _object(selection["controller"])
    _bounded_bytes(controller_manifest_bytes)
    manifest_sha256 = hashlib.sha256(controller_manifest_bytes).hexdigest()
    if not hmac.compare_digest(manifest_sha256, _digest(controller["manifest_sha256"])):
        raise ValueError("raw detached manifest digest disagrees with selection")
    entrypoint, members = _manifest(controller_manifest_bytes)
    selected_rows = controller["members"]
    if not isinstance(selected_rows, list):
        raise ValueError("selection controller members are invalid")
    if any(type(_object(row)["size_bytes"]) is not int for row in selected_rows):
        raise ValueError("selection controller member sizes must be exact integers")
    selected_members = tuple(
        {key: value for key, value in _object(row).items() if key != "role"} for row in selected_rows
    )
    if controller["entrypoint"] != entrypoint or selected_members != tuple(
        {"path": member.path, "size_bytes": member.size, "sha256": member.sha256} for member in members
    ):
        raise ValueError("detached manifest disagrees with selected controller")
    return _SelectionManifest(selection, selection_sha256, manifest_sha256, entrypoint, members)


def validate_retained_selection(
    capture: SelectionBoundControllerCapture,
    *,
    anchor: ExternalSelectionAnchor,
    deployment: BootstrapDeploymentPolicy,
    now: datetime,
) -> dict[str, JsonValue]:
    """Recheck current host-supplied pin/policy against retained bytes, without execution.

    The independently controlled caller supplies current authority and time.
    This pure comparison does not establish that caller's approval or deployment.
    No source tree is reopened and no retained dataclass supplies its own trust.
    """
    if not isinstance(capture, SelectionBoundControllerCapture):
        raise ValueError("retained selection capture is invalid")
    validated = _selection_manifest(
        capture.selection_bytes,
        capture.controller_manifest_bytes,
        anchor=anchor,
        deployment=deployment,
        now=now,
    )
    if (
        capture.selection_sha256 != validated.selection_sha256
        or capture.controller_manifest_sha256 != validated.manifest_sha256
        or capture.anchor_reference != anchor.approval_reference
        or type(capture.anchor_generation) is not int
        or capture.anchor_generation != anchor.generation
    ):
        raise ValueError("retained selection provenance disagrees with current anchor")
    retained = validate_controller_capture(capture.controller)
    if capture.controller.entrypoint != validated.entrypoint or len(retained) != len(validated.members):
        raise ValueError("retained controller disagrees with detached manifest")
    for (path, raw), expected in zip(retained, validated.members, strict=True):
        if (
            path != expected.path
            or len(raw) != expected.size
            or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected.sha256)
        ):
            raise ValueError("retained controller disagrees with detached manifest")
    _initial_export(ControllerCapture(capture.controller.entrypoint, retained))
    return validated.selection


def authenticate_initial_export_selection(
    selection_bytes: bytes,
    controller_manifest_bytes: bytes,
    *,
    anchor: ExternalSelectionAnchor,
    deployment: BootstrapDeploymentPolicy,
    controller_root: Path,
    now: datetime,
) -> SelectionBoundControllerCapture:
    """Compare an external pin, deployed schemas and closed C0 bytes, without execution.

    The host must independently authorize and supply deployment/anchor state.
    Errors return no accepted capture. This API neither launches C0 nor verifies
    runtime contents, observations, revocation data, signatures or a release.
    """
    validated = _selection_manifest(
        selection_bytes, controller_manifest_bytes, anchor=anchor, deployment=deployment, now=now
    )
    capture = capture_controller_closure(
        controller_root, expected_entrypoint=validated.entrypoint, expected_members=validated.members
    )
    _initial_export(capture)
    return SelectionBoundControllerCapture(
        selection_bytes,
        validated.selection_sha256,
        controller_manifest_bytes,
        validated.manifest_sha256,
        anchor.approval_reference,
        anchor.generation,
        capture,
    )
