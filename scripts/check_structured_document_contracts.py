"""Validate every policy-selected structured document through its explicit owner."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

import click
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from fieldkit.util.bounded_process import BoundedProcessError

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import (
    _release_governance,
    _release_policy,
    _supply_chain_policy,
    check_artifacts,
    check_compatibility_policy,
    check_dependency_profiles,
    check_documentation_contract,
    check_public_docs,
    check_public_identity,
    export_public_tree,
    public_tree_scan,
    quality_source,
)
from scripts.json_policy import load_json_bytes
from scripts.rehearsal_acquisition import read_root_bytes

OwnerKind = Literal[
    "documentation",
    "artifact",
    "compatibility",
    "dependencies",
    "supply-chain",
    "identity",
    "surface",
    "tree",
    "scan",
    "governance",
    "release",
    "schema",
]
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
SCHEMA_BASE = "https://github.com/mpeter/fieldkit-cli/blob/main/docs/release-readiness/"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True)
class Owner:
    path: str
    kind: OwnerKind


OWNERS = (
    Owner("docs/documentation-contract.json", "documentation"),
    Owner("docs/release-readiness/artifact-policy.json", "artifact"),
    Owner("docs/release-readiness/compatibility-policy.json", "compatibility"),
    Owner("docs/release-readiness/dependency-ownership.json", "dependencies"),
    Owner("docs/release-readiness/dependency-security-policy.json", "supply-chain"),
    Owner("docs/release-readiness/public-identity-policy.json", "identity"),
    Owner("docs/release-readiness/public-surface-policy.json", "surface"),
    Owner("docs/release-readiness/public-tree-policy.json", "tree"),
    Owner("docs/release-readiness/public-tree-scan-policy.json", "scan"),
    Owner("docs/release-readiness/release-governance-policy.json", "governance"),
    Owner("docs/release-readiness/release-policy.json", "release"),
    *(
        Owner(f"docs/release-readiness/{name}.schema.json", "schema")
        for name in (
            "cutover-record",
            "documentation-contract",
            "public-history-source",
            "public-tree-manifest",
            "public-tree-policy",
            "quality-gate-receipt",
            "rehearsal-evidence",
            "release-approval-input",
            "release-bundle-provenance",
            "release-consumer-evidence",
            "release-controller-receipt",
            "release-evidence-record",
            "release-governance-policy",
            "release-manual-evidence",
            "release-promotion-evidence",
            "release-trust-selection",
        )
    ),
)


def _inventory(repo: Path) -> tuple[str, ...]:
    data = quality_source._git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "docs")
    return tuple(
        sorted(
            {
                name.decode("utf-8")
                for name in data.split(b"\0")
                if name and Path(name.decode("utf-8")).suffix.lower() in {".json", ".yaml", ".yml", ".toml"}
            }
        )
    )


def _object(repo: Path, path: str) -> dict[str, object]:
    data = read_root_bytes(repo, path)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("structured document exceeds byte limit")
    value = load_json_bytes(data)
    if not isinstance(value, dict):
        raise ValueError("structured document must be an object")
    stack: list[object] = [value]
    while stack:
        child = stack.pop()
        if isinstance(child, float) and not math.isfinite(child):
            raise ValueError("structured document contains a nonfinite value")
        if isinstance(child, dict):
            stack.extend(child.values())
        elif isinstance(child, list):
            stack.extend(child)
    return value


def _schema_nodes(schema: dict[str, object]) -> list[dict[str, object]]:
    nodes = [schema]
    for node in nodes:
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            value = node.get(key)
            if isinstance(value, dict):
                nodes.extend(child for child in value.values() if isinstance(child, dict))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            value = node.get(key)
            if isinstance(value, list):
                nodes.extend(child for child in value if isinstance(child, dict))
        for key in (
            "additionalProperties",
            "unevaluatedProperties",
            "propertyNames",
            "contains",
            "items",
            "unevaluatedItems",
            "not",
            "if",
            "then",
            "else",
            "contentSchema",
        ):
            value = node.get(key)
            if isinstance(value, dict):
                nodes.append(value)
    return nodes


def _schemas(repo: Path, owners: tuple[Owner, ...]) -> None:
    schemas = {
        SCHEMA_BASE + Path(owner.path).name: _object(repo, owner.path) for owner in owners if owner.kind == "schema"
    }
    for identifier, schema in schemas.items():
        if schema.get("$schema") != SCHEMA_DIALECT or schema.get("$id") != identifier:
            raise ValueError("structured schema identity or dialect is invalid")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise ValueError("structured schema shape is invalid") from None
        for index, node in enumerate(_schema_nodes(schema)):
            if "$dynamicRef" in node or "$dynamicAnchor" in node or (index and ("$id" in node or "$schema" in node)):
                raise ValueError("structured schema has unsupported resource semantics")
    registry: Registry = Registry().with_resources(
        (identifier, Resource.from_contents(schema)) for identifier, schema in schemas.items()
    )
    for identifier, schema in schemas.items():
        for node in _schema_nodes(schema):
            reference = node.get("$ref")
            if reference is None:
                continue
            if not isinstance(reference, str) or urljoin(identifier, reference).split("#", 1)[0] not in schemas:
                raise ValueError("structured schema reference is not a selected local resource")
            try:
                registry.resolver(identifier).lookup(reference)
            except Unresolvable:
                raise ValueError("structured schema reference is unresolved") from None


def _validate_owner(repo: Path, owner: Owner) -> None:
    if owner.kind == "documentation":
        valid = not check_documentation_contract.validate(repo)
    elif owner.kind == "artifact":
        check_artifacts.load_policy(repo)
        valid = True
    elif owner.kind == "compatibility":
        valid = check_compatibility_policy.validate(repo).ok
    elif owner.kind == "dependencies":
        valid = check_dependency_profiles.validate(repo).ok
    elif owner.kind == "supply-chain":
        valid = _supply_chain_policy.validate_repository(repo).ok
    elif owner.kind == "identity":
        valid = check_public_identity.validate(repo).ok
    elif owner.kind == "surface":
        valid = not (*check_public_docs.validate_sources(repo), *check_public_docs.validate_community(repo))
    elif owner.kind == "tree":
        export_public_tree._load_policy(repo / owner.path)
        valid = True
    elif owner.kind == "scan":
        public_tree_scan._load_policy(read_root_bytes(repo, owner.path), datetime.now(tz=UTC).date())
        valid = True
    elif owner.kind == "governance":
        _release_governance.load_policy(repo / owner.path)
        valid = True
    elif owner.kind == "release":
        valid = _release_policy.validate_repository(repo).ok
    else:
        raise ValueError("structured document has an unsupported owner")
    if not valid:
        raise ValueError("structured document canonical validation failed")


def check(repo: Path) -> tuple[str, ...]:
    """Return validated paths; this establishes document contracts, never release authority."""
    paths = _inventory(repo)
    policy = export_public_tree._load_policy(repo / "docs/release-readiness/public-tree-policy.json")
    included, _ = export_public_tree._classify(tuple((path, "100644", "0" * 40) for path in paths), policy)
    selected = {entry.path for entry in included}
    declared = [owner.path for owner in OWNERS]
    if len(declared) != len(set(declared)):
        raise ValueError("structured document has ambiguous ownership")
    if selected != set(declared):
        raise ValueError("structured document ownership is missing or stale")
    for owner in OWNERS:
        _object(repo, owner.path)
        if owner.kind != "schema":
            _validate_owner(repo, owner)
    _schemas(repo, OWNERS)
    return tuple(sorted(selected))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        paths = check(args.repo)
    except (OSError, ValueError, Unresolvable, BoundedProcessError):
        click.echo("Structured document contracts: FAIL")
        return 1
    click.echo(f"Structured document contracts: PASS ({len(paths)} owned documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
