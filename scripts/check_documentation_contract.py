#!/usr/bin/env python3
"""Enforce fieldkit's versioned public-documentation contract."""

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_SURFACE_POLICY_PATH = Path("docs/release-readiness/public-surface-policy.json")
_SCHEMA_PATH = Path("docs/release-readiness/documentation-contract.schema.json")
_CONTENT_TYPES = frozenset({"concept", "how-to", "overview", "policy", "reference", "roadmap", "tutorial"})
_FUTURE_STATUS = re.compile(
    r"(?i)\b(?:in[ -]flight|backlog|coming soon|under development|we will support|"
    r"future (?:contributors?|release|work)|pending (?:implementation|release|support|work)|"
    r"not yet (?:implemented|published|supported)|planned (?:first |public )?(?:release|work))\b"
)
_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_TABLE_SEPARATOR = re.compile(r"^ {0,3}\|?(?:[ :]?-{3,}[ :]?\|)+(?:[ :]?-{3,}[ :]?)?$")
_VERIFICATION_EVIDENCE = {
    "artifact_smoke": "make artifact-check",
    "contributor_gate": "QUALITY_BASE=upstream/main make pr-check",
    "generated_dependency_map": "uv run python scripts/generate_dep_map.py --check",
    "generated_reference": "uv run python scripts/generate_cli_docs.py --check",
    "live_cutover": "ROADMAP.md",
    "policy_validator": "make quality",
    "source_contract": "uv run python scripts/check_documentation_contract.py",
}
_AUTOMATED_EXAMPLE_CLASSES = frozenset({"generated_reference", "safe_automated_command", "structural_assertion"})
_MANUAL_EXAMPLE_CLASSES = frozenset({"credentialed_manual_integration", "exact_release_cutover_proof"})
_EXAMPLE_VERIFICATION_EVIDENCE = {
    "automated.roadmap-contract": (
        "structural_assertion",
        "automated",
        "uv run python scripts/check_roadmap_contract.py",
        ("scripts/check_roadmap_contract.py",),
    ),
    "automated.compatibility-policy": (
        "structural_assertion",
        "automated",
        "uv run python scripts/check_compatibility_policy.py",
        ("scripts/check_compatibility_policy.py",),
    ),
    "automated.configuration-example-contract": (
        "structural_assertion",
        "automated",
        "uv run pytest tests/test_documentation_configuration_examples.py -q",
        ("tests/test_documentation_configuration_examples.py",),
    ),
    "automated.exit-code-contract": (
        "structural_assertion",
        "automated",
        "uv run pytest tests/test_cli_exit.py -q",
        ("tests/test_cli_exit.py",),
    ),
    "automated.generated-dependency-map": (
        "generated_reference",
        "automated",
        "dependency-map documentation generator",
        ("scripts/generate_dep_map.py",),
    ),
    "automated.generated-reference": (
        "generated_reference",
        "automated",
        "canonical documentation generator",
        ("scripts/generate_cli_docs.py",),
    ),
    "automated.installed-base-artifact": (
        "safe_automated_command",
        "automated",
        "fixed installed-artifact documentation scenarios",
        ("scripts/check_documentation_example_scenarios.py", "scripts/smoke_artifact.py"),
    ),
    "automated.release-workflow-policy": (
        "safe_automated_command",
        "automated",
        "make release-workflow-policy-check",
        ("Makefile", "scripts/release_workflow_policy.py"),
    ),
    "manual.credentialed-integration": (
        "credentialed_manual_integration",
        "manual_evidence",
        "docs/release-readiness/rehearsal-evidence.schema.json",
        ("docs/release-readiness/rehearsal-evidence.schema.json",),
    ),
    "manual.release-cutover": (
        "exact_release_cutover_proof",
        "manual_evidence",
        "docs/release-readiness/rehearsal-evidence.schema.json",
        ("docs/release-readiness/rehearsal-evidence.schema.json",),
    ),
}


@dataclass(frozen=True, order=True)
class Finding:
    """One documentation-contract violation."""

    criterion_id: str
    path: str


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while rejecting ambiguous duplicate JSON keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    """Load one contract object with strict duplicate-key handling."""
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _public_markdown_paths(repo_root: Path) -> frozenset[str]:
    """Resolve every Markdown file classified for the public repository."""
    policy = _load_json(repo_root / _SURFACE_POLICY_PATH)
    categories = policy.get("categories")
    if not isinstance(categories, dict):
        raise ValueError(f"{_SURFACE_POLICY_PATH}: categories must be an object")
    patterns: list[str] = []
    for category in ("public_entrypoint", "public_site", "public_repository_only"):
        values = categories.get(category)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{_SURFACE_POLICY_PATH}: {category} must be a string list")
        patterns.extend(values)
    candidates = (
        path.relative_to(repo_root).as_posix() for path in repo_root.rglob("*.md") if ".git" not in path.parts
    )
    return frozenset(path for path in candidates if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns))


def _private_patterns(repo_root: Path) -> tuple[str, ...]:
    """Return source patterns excluded from the clean public repository."""
    policy = _load_json(repo_root / _SURFACE_POLICY_PATH)
    categories = policy.get("categories")
    values = categories.get("private_history_excluded") if isinstance(categories, dict) else None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{_SURFACE_POLICY_PATH}: private_history_excluded must be a string list")
    return tuple(value for value in values if isinstance(value, str))


def _source_files(repo_root: Path, patterns: list[str]) -> tuple[Path, ...]:
    """Resolve contract source patterns without following source symlinks."""
    matches: set[Path] = set()
    for pattern in patterns:
        for path in repo_root.glob(pattern):
            if path.is_symlink():
                raise ValueError(f"{_CONTRACT_PATH}: source patterns must not resolve through symlinks")
            if path.is_file():
                matches.add(path)
    return tuple(sorted(matches))


def _validate_source_patterns(patterns: list[str], document: str) -> None:
    """Reject source patterns that can escape the repository root."""
    for pattern in patterns:
        relative = PurePosixPath(pattern)
        if not pattern or pattern.startswith("/") or "\\" in pattern or ".." in relative.parts:
            raise ValueError(f"{_CONTRACT_PATH}: {document} sources must be repository-relative patterns")


def _has_unresolved_pattern(repo_root: Path, patterns: list[str]) -> bool:
    """Report whether any declared source pattern matches no file."""
    return any(not any(path.is_file() for path in repo_root.glob(pattern)) for pattern in patterns)


def _fingerprint(repo_root: Path, patterns: list[str]) -> str:
    """Hash stable repository-relative names and contents for source inputs."""
    digest = hashlib.sha256()
    for path in _source_files(repo_root, patterns):
        digest.update(path.relative_to(repo_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _block_inventory(document: Path) -> list[dict[str, str]]:
    """Inventory CommonMark fenced blocks by language and reviewed content."""
    inventory: list[dict[str, str]] = []
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    index = 0
    while index < len(lines):
        opening = _FENCE_START.fullmatch(lines[index].rstrip("\r\n"))
        if opening is None:
            index += 1
            continue
        marker = opening.group("marker")
        info = opening.group("info").strip()
        language = info.split(maxsplit=1)[0] if info else ""
        closing = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\r?\n)?$")
        body_start = index + 1
        index = body_start
        while index < len(lines) and closing.fullmatch(lines[index]) is None:
            index += 1
        if index == len(lines):
            raise ValueError(f"{document}: unclosed fenced block")
        body = "".join(lines[body_start:index])
        digest = hashlib.sha256(f"{language}\0{body}".encode()).hexdigest()
        inventory.append({"language": language, "sha256": digest})
        index += 1
    return inventory


def _table_inventory(document: Path) -> list[dict[str, str]]:
    """Inventory complete pipe-table structures by their reviewed content."""
    inventory: list[dict[str, str]] = []
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    index = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or _TABLE_SEPARATOR.fullmatch(lines[index + 1].rstrip("\r\n")) is None:
            index += 1
            continue
        table_start = index
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            index += 1
        digest = hashlib.sha256("".join(lines[table_start:index]).encode()).hexdigest()
        inventory.append({"sha256": digest})
    return inventory


def _documents(contract: dict[str, object]) -> dict[str, dict[str, object]]:
    """Validate the contract envelope and return its document records."""
    if contract.get("schema_version") != 2:
        raise ValueError(f"{_CONTRACT_PATH}: schema_version must be 2")
    raw_documents = contract.get("documents")
    if not isinstance(raw_documents, dict):
        raise ValueError(f"{_CONTRACT_PATH}: documents must be an object")
    documents: dict[str, dict[str, object]] = {}
    for path, value in raw_documents.items():
        if not isinstance(path, str) or not isinstance(value, dict):
            raise ValueError(f"{_CONTRACT_PATH}: document entries must be objects keyed by paths")
        documents[path] = value
    return documents


def _validate_schema(repo_root: Path, contract: dict[str, object]) -> None:
    """Validate the complete contract against its checked JSON Schema."""
    schema = _load_json(repo_root / _SCHEMA_PATH)
    json_instance = json.loads(json.dumps(contract))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(json_instance), key=lambda error: list(error.absolute_path)
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    raise ValueError(f"{_CONTRACT_PATH}: schema violation at {location}: {first.message}")


def _validate_example_ownership(contract: dict[str, object], documents: dict[str, dict[str, object]]) -> None:
    """Require stable, unique ownership for every fenced block and table."""
    raw_verifications = contract.get("example_verifications")
    if not isinstance(raw_verifications, dict):
        raise ValueError(f"{_CONTRACT_PATH}: example_verifications must be an object")
    if set(raw_verifications) != set(_EXAMPLE_VERIFICATION_EVIDENCE):
        raise ValueError(f"{_CONTRACT_PATH}: example verifications must be the supported set")
    for verification_id, expected in _EXAMPLE_VERIFICATION_EVIDENCE.items():
        verification = raw_verifications.get(verification_id)
        if not isinstance(verification, dict):
            raise ValueError(f"{_CONTRACT_PATH}: missing example verification: {verification_id}")
        classification, mode, evidence, _ = expected
        if verification != {"classification": classification, "mode": mode, "evidence": evidence}:
            raise ValueError(f"{_CONTRACT_PATH}: unsupported example verification route: {verification_id}")
    identifiers: list[str] = []
    for path, entry in documents.items():
        for field, subject in (("fenced_blocks", "fenced block"), ("tables", "table")):
            records = entry.get(field, [])
            if not isinstance(records, list):
                raise ValueError(f"{_CONTRACT_PATH}: {path} {field} must be a list")
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"{_CONTRACT_PATH}: {path} {subject} entries must be objects")
                identifier = record.get("id")
                classification = record.get("classification")
                verification_id = record.get("verification_id")
                if not isinstance(identifier, str) or not isinstance(verification_id, str):
                    raise ValueError(f"{_CONTRACT_PATH}: {path} {subject}s need stable ownership identifiers")
                identifiers.append(identifier)
                verification = raw_verifications.get(verification_id)
                if not isinstance(verification, dict) or verification.get("classification") != classification:
                    raise ValueError(
                        f"{_CONTRACT_PATH}: {identifier} has unknown or incompatible verification ownership"
                    )
                mode = verification.get("mode")
                if classification in _AUTOMATED_EXAMPLE_CLASSES and mode != "automated":
                    raise ValueError(f"{_CONTRACT_PATH}: {identifier} automated verification has an unsafe mode")
                if classification in _MANUAL_EXAMPLE_CLASSES and mode != "manual_evidence":
                    raise ValueError(f"{_CONTRACT_PATH}: {identifier} manual verification has an unsafe mode")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{_CONTRACT_PATH}: fenced block identifiers must be unique")


def _validate_verification_ownership(contract: dict[str, object], documents: dict[str, dict[str, object]]) -> None:
    """Require exactly one supported verification owner per public document."""
    raw = contract.get("verification")
    if not isinstance(raw, dict) or set(raw) != set(_VERIFICATION_EVIDENCE):
        raise ValueError(f"{_CONTRACT_PATH}: verification mechanisms must be the supported set")
    owners: list[str] = []
    for mechanism, value in raw.items():
        if not isinstance(value, dict) or set(value) != {"evidence", "paths"}:
            raise ValueError(f"{_CONTRACT_PATH}: verification entries need evidence and paths")
        paths = value.get("paths")
        if (
            value.get("evidence") != _VERIFICATION_EVIDENCE[mechanism]
            or not isinstance(paths, list)
            or not all(isinstance(path, str) for path in paths)
        ):
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence must match the enforced route")
        owners.extend(path for path in paths if isinstance(path, str))
    if len(owners) != len(set(owners)) or set(owners) != set(documents):
        raise ValueError(f"{_CONTRACT_PATH}: every document needs exactly one verification owner")


def _validate_verification_evidence(repo_root: Path) -> None:
    """Ensure every declared verification route exists in the repository."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    required_targets = {
        "artifact-check:": "make artifact-check",
        "pr-check:": "QUALITY_BASE=upstream/main make pr-check",
        "quality:": "make quality",
    }
    for target, evidence in required_targets.items():
        if target not in makefile:
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence is unavailable: {evidence}")
    required_files = (
        "docs/release-readiness/documentation-contract.schema.json",
        "scripts/generate_cli_docs.py",
        "scripts/generate_dep_map.py",
        "scripts/check_documentation_contract.py",
        "scripts/check_documentation_examples.py",
        "ROADMAP.md",
    )
    for relative in required_files:
        if not (repo_root / relative).is_file():
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence is unavailable: {relative}")
    for _, _, _, evidence_paths in _EXAMPLE_VERIFICATION_EVIDENCE.values():
        for relative in evidence_paths:
            if not (repo_root / relative).is_file():
                raise ValueError(f"{_CONTRACT_PATH}: example verification evidence is unavailable: {relative}")


def validate(repo_root: Path = _REPO_ROOT) -> tuple[Finding, ...]:
    """Return all fail-closed public-documentation contract findings."""
    contract = _load_json(repo_root / _CONTRACT_PATH)
    _validate_schema(repo_root, contract)
    documents = _documents(contract)
    _validate_verification_ownership(contract, documents)
    _validate_example_ownership(contract, documents)
    _validate_verification_evidence(repo_root)
    roadmap = contract.get("roadmap")
    if not isinstance(roadmap, str) or documents.get(roadmap, {}).get("content_type") != "roadmap":
        raise ValueError(f"{_CONTRACT_PATH}: roadmap must name the roadmap document")
    findings: list[Finding] = []
    public_paths = _public_markdown_paths(repo_root)
    private_patterns = _private_patterns(repo_root)
    for path in sorted(public_paths - documents.keys()):
        findings.append(Finding("DOC401", path))
    for path in sorted(documents.keys() - public_paths):
        findings.append(Finding("DOC402", path))
    for path, entry in sorted(documents.items()):
        document = repo_root / path
        if not document.is_file():
            findings.append(Finding("DOC403", path))
            continue
        content_type = entry.get("content_type")
        reader_action = entry.get("reader_action")
        sources = entry.get("sources")
        if content_type not in _CONTENT_TYPES or not isinstance(reader_action, str) or not reader_action.strip():
            findings.append(Finding("DOC404", path))
            continue
        if not isinstance(sources, list) or not sources or not all(isinstance(source, str) for source in sources):
            findings.append(Finding("DOC405", path))
            continue
        source_patterns = [source for source in sources if isinstance(source, str)]
        _validate_source_patterns(source_patterns, path)
        if _has_unresolved_pattern(repo_root, source_patterns):
            findings.append(Finding("DOC405", path))
            continue
        source_paths = _source_files(repo_root, source_patterns)
        if any(
            any(fnmatch.fnmatchcase(source.relative_to(repo_root).as_posix(), pattern) for pattern in private_patterns)
            for source in source_paths
        ):
            findings.append(Finding("DOC409", path))
        if entry.get("source_fingerprint") != _fingerprint(repo_root, source_patterns):
            findings.append(Finding("DOC406", path))
        if path != roadmap and _FUTURE_STATUS.search(document.read_text(encoding="utf-8")):
            findings.append(Finding("DOC407", path))
        declared_blocks = entry.get("fenced_blocks")
        reviewed_inventory = (
            [{"language": block.get("language"), "sha256": block.get("sha256")} for block in declared_blocks]
            if isinstance(declared_blocks, list) and all(isinstance(block, dict) for block in declared_blocks)
            else None
        )
        if reviewed_inventory != _block_inventory(document):
            findings.append(Finding("DOC408", path))
        declared_tables = entry.get("tables", [])
        reviewed_tables = (
            [{"sha256": table.get("sha256")} for table in declared_tables]
            if isinstance(declared_tables, list) and all(isinstance(table, dict) for table in declared_tables)
            else None
        )
        if reviewed_tables != _table_inventory(document):
            findings.append(Finding("DOC410", path))
    return tuple(sorted(findings))


def refresh_fingerprints(repo_root: Path = _REPO_ROOT) -> None:
    """Refresh source fingerprints after a human reviews affected documents."""
    contract_path = repo_root / _CONTRACT_PATH
    contract = _load_json(contract_path)
    for path, entry in _documents(contract).items():
        sources = entry.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(source, str) for source in sources):
            raise ValueError(f"{_CONTRACT_PATH}: {path} has invalid sources")
        source_patterns = [source for source in sources if isinstance(source, str)]
        _validate_source_patterns(source_patterns, path)
        if _has_unresolved_pattern(repo_root, source_patterns):
            raise ValueError(f"{_CONTRACT_PATH}: {path} has an unresolved source pattern")
        entry["source_fingerprint"] = _fingerprint(repo_root, source_patterns)
    temporary = contract_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(contract_path)


def refresh_block_inventory(repo_root: Path = _REPO_ROOT) -> None:
    """Refresh fenced-block hashes after every example has been reviewed."""
    contract_path = repo_root / _CONTRACT_PATH
    contract = _load_json(contract_path)
    for path, entry in _documents(contract).items():
        document = repo_root / path
        if not document.is_file():
            raise ValueError(f"{_CONTRACT_PATH}: {path} does not exist")
        current = entry.get("fenced_blocks")
        inventory = _block_inventory(document)
        if not isinstance(current, list) or len(current) != len(inventory):
            raise ValueError(f"{_CONTRACT_PATH}: {path} block ownership must be reviewed before refreshing hashes")
        refreshed: list[dict[str, object]] = []
        for declared, observed in zip(current, inventory, strict=True):
            if not isinstance(declared, dict) or not {"id", "classification", "verification_id"} <= set(declared):
                raise ValueError(f"{_CONTRACT_PATH}: {path} block ownership must be reviewed before refreshing hashes")
            reviewed = {"language": declared.get("language"), "sha256": declared.get("sha256")}
            if reviewed != observed:
                raise ValueError(f"{_CONTRACT_PATH}: {path} changed blocks require explicit ownership review")
            refreshed.append(declared)
        entry["fenced_blocks"] = refreshed
    temporary = contract_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(contract_path)


def main(argv: list[str] | None = None) -> int:
    """Run the contract check or explicitly refresh reviewed fingerprints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-blocks", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.refresh:
            refresh_fingerprints(args.repo_root)
        if args.refresh_blocks:
            refresh_block_inventory(args.repo_root)
        findings = validate(args.repo_root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Documentation contract: ERROR: {error}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"{finding.criterion_id}: {finding.path}")
        return 1
    print("Documentation contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
