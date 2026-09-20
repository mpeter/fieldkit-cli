#!/usr/bin/env python3
"""Validate fieldkit's dependency-profile ownership contract without importing fieldkit."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = Path("docs/release-readiness/dependency-ownership.json")
PYPROJECT_PATH = Path("pyproject.toml")
DISPATCHER_PATH = Path("src/fieldkit/__main__.py")
SOURCE_PATH = Path("src/fieldkit")

_OWNING_PROFILES = ("base", "google", "llm", "web", "chrome-auth")
_OPTIONAL_PROFILES = ("google", "llm", "web", "chrome-auth")
_COMPOSED_PROFILE = "all"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, order=True)
class Finding:
    """One deterministic dependency-profile contract violation."""

    rule_id: str
    subject: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """Versioned output shared by the human and JSON renderers."""

    schema_version: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "findings": [asdict(finding) for finding in self.findings],
        }


def _object_map(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _string_list(value: object, subject: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{subject} must be a list of non-empty strings")
    return [item for item in value if isinstance(item, str)]


def _canonical_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        raise ValueError(f"unsupported requirement syntax: {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _load_manifest(repo_root: Path) -> dict[str, object]:
    value = json.loads((repo_root / MANIFEST_PATH).read_text(encoding="utf-8"))
    return _object_map(value, str(MANIFEST_PATH))


def _load_pyproject(repo_root: Path) -> dict[str, object]:
    with (repo_root / PYPROJECT_PATH).open("rb") as stream:
        value = tomllib.load(stream)
    return _object_map(value, str(PYPROJECT_PATH))


def _profile_requirements(profile: dict[str, object], subject: str) -> set[str]:
    return {_canonical_name(item) for item in _string_list(profile.get("requirements"), subject)}


def _metadata_requirements(value: object, subject: str) -> dict[str, str]:
    requirements = _string_list(value, subject)
    by_name: dict[str, str] = {}
    for requirement in requirements:
        name = _canonical_name(requirement)
        if name in by_name:
            raise ValueError(f"{subject} declares {name!r} more than once")
        by_name[name] = requirement
    return by_name


def _command_names(repo_root: Path) -> set[str]:
    value = _literal_assignment(repo_root, "_COMMANDS")
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{DISPATCHER_PATH}: _COMMANDS must be a literal mapping with string keys")
    return {key for key in value if isinstance(key, str)}


def _literal_assignment(repo_root: Path, name: str) -> object:
    tree = ast.parse((repo_root / DISPATCHER_PATH).read_text(encoding="utf-8"), filename=str(DISPATCHER_PATH))
    for node in tree.body:
        match node:
            case ast.AnnAssign(target=ast.Name(id=assignment_name), value=assignment_value) if assignment_name == name:
                pass
            case ast.Assign(targets=targets, value=assignment_value) if any(
                isinstance(target, ast.Name) and target.id == name for target in targets
            ):
                pass
            case _:
                assignment_value = None
        if assignment_value is not None:
            return ast.literal_eval(assignment_value)
    raise ValueError(f"{DISPATCHER_PATH}: literal {name} assignment not found")


def _dispatcher_profiles(repo_root: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    value = _literal_assignment(repo_root, "_OPTIONAL_COMMAND_PROFILES")
    if not isinstance(value, dict):
        raise ValueError(f"{DISPATCHER_PATH}: _OPTIONAL_COMMAND_PROFILES must be a literal mapping")
    profiles: dict[str, tuple[str, tuple[str, ...]]] = {}
    for command, raw_spec in value.items():
        if (
            not isinstance(command, str)
            or not isinstance(raw_spec, tuple)
            or len(raw_spec) != 2
            or not isinstance(raw_spec[0], str)
            or not isinstance(raw_spec[1], tuple)
            or not all(isinstance(root, str) for root in raw_spec[1])
        ):
            raise ValueError(f"{DISPATCHER_PATH}: invalid optional command profile entry {command!r}")
        profiles[command] = (raw_spec[0], tuple(root for root in raw_spec[1] if isinstance(root, str)))
    return profiles


def _third_party_imports(repo_root: Path) -> set[str]:
    modules: set[str] = set()
    for path in sorted((repo_root / SOURCE_PATH).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(repo_root)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module)
    return {
        module
        for module in modules
        if module.split(".", maxsplit=1)[0] not in sys.stdlib_module_names
        and module.split(".", maxsplit=1)[0] not in {"__future__", "fieldkit"}
    }


def _add_set_difference(
    findings: list[Finding],
    *,
    rule_id: str,
    subject: str,
    expected: set[str],
    actual: set[str],
) -> None:
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        findings.append(Finding(rule_id, subject, f"missing: {', '.join(missing)}"))
    if unexpected:
        findings.append(Finding(rule_id, subject, f"unexpected: {', '.join(unexpected)}"))


def validate(repo_root: Path = REPO_ROOT) -> ValidationReport:
    """Return every ownership violation for *repo_root* without network access."""
    manifest = _load_manifest(repo_root)
    pyproject = _load_pyproject(repo_root)
    profiles = _object_map(manifest.get("profiles"), "manifest profiles")
    expected_profile_names = {*_OWNING_PROFILES, _COMPOSED_PROFILE}
    findings: list[Finding] = []
    _add_set_difference(
        findings,
        rule_id="DEP001",
        subject="manifest profiles",
        expected=expected_profile_names,
        actual=set(profiles),
    )
    if findings:
        return ValidationReport(schema_version=1, findings=tuple(sorted(findings)))

    owner_requirements: dict[str, set[str]] = {}
    owner_roots: dict[str, set[str]] = {}
    for profile_name in _OWNING_PROFILES:
        profile = _object_map(profiles[profile_name], f"profile {profile_name}")
        owner_requirements[profile_name] = _profile_requirements(profile, f"profile {profile_name} requirements")
        owner_roots[profile_name] = set(_string_list(profile.get("import_roots"), f"profile {profile_name} roots"))

    requirement_owners: dict[str, list[str]] = {}
    root_owners: dict[str, list[str]] = {}
    for profile_name in _OWNING_PROFILES:
        for requirement in owner_requirements[profile_name]:
            requirement_owners.setdefault(requirement, []).append(profile_name)
        for root in owner_roots[profile_name]:
            root_owners.setdefault(root, []).append(profile_name)
    for requirement, owners in sorted(requirement_owners.items()):
        if len(owners) != 1:
            findings.append(Finding("DEP002", requirement, f"owned by: {', '.join(owners)}"))
    for root, owners in sorted(root_owners.items()):
        if len(owners) != 1:
            findings.append(Finding("DEP003", root, f"owned by: {', '.join(owners)}"))

    project = _object_map(pyproject.get("project"), "pyproject project")
    metadata_profiles: dict[str, dict[str, str]] = {
        "base": _metadata_requirements(project.get("dependencies"), "project dependencies")
    }
    optional = _object_map(project.get("optional-dependencies"), "project optional-dependencies")
    for profile_name in (*_OPTIONAL_PROFILES, _COMPOSED_PROFILE):
        if profile_name not in optional:
            findings.append(Finding("DEP004", profile_name, "missing from project optional-dependencies"))
            metadata_profiles[profile_name] = {}
            continue
        metadata_profiles[profile_name] = _metadata_requirements(
            optional[profile_name], f"optional dependency profile {profile_name}"
        )

    for profile_name in _OWNING_PROFILES:
        _add_set_difference(
            findings,
            rule_id="DEP005",
            subject=profile_name,
            expected=owner_requirements[profile_name],
            actual=set(metadata_profiles[profile_name]),
        )

    all_profile = _object_map(profiles[_COMPOSED_PROFILE], "profile all")
    composed = set(_string_list(all_profile.get("composes"), "profile all composes"))
    _add_set_difference(
        findings,
        rule_id="DEP006",
        subject="all compositions",
        expected=set(_OPTIONAL_PROFILES),
        actual=composed,
    )
    manifest_union = set().union(*(owner_requirements[name] for name in _OPTIONAL_PROFILES))
    _add_set_difference(
        findings,
        rule_id="DEP007",
        subject="manifest all requirements",
        expected=manifest_union,
        actual=_profile_requirements(all_profile, "profile all requirements"),
    )
    metadata_union = {
        requirement for profile_name in _OPTIONAL_PROFILES for requirement in metadata_profiles[profile_name].values()
    }
    actual_all_specs = set(metadata_profiles[_COMPOSED_PROFILE].values())
    if metadata_union != actual_all_specs:
        findings.append(Finding("DEP008", "metadata all", "must exactly equal the optional-profile requirement union"))

    command_groups = _object_map(manifest.get("command_groups"), "manifest command_groups")
    _add_set_difference(
        findings,
        rule_id="DEP009",
        subject="command groups",
        expected=_command_names(repo_root),
        actual=set(command_groups),
    )
    for command, raw_entry in sorted(command_groups.items()):
        entry = _object_map(raw_entry, f"command group {command}")
        entry_profile = entry.get("entry_profile")
        if not isinstance(entry_profile, str) or entry_profile not in _OWNING_PROFILES:
            findings.append(Finding("DEP010", command, f"invalid entry_profile: {entry_profile!r}"))
        optional_profiles = set(
            _string_list(entry.get("optional_profiles"), f"command group {command} optional_profiles")
        )
        invalid_optional = sorted(optional_profiles - set(_OPTIONAL_PROFILES))
        if invalid_optional:
            findings.append(Finding("DEP011", command, f"invalid optional profiles: {', '.join(invalid_optional)}"))

    expected_dispatcher_profiles: dict[str, tuple[str, tuple[str, ...]]] = {}
    for command, raw_entry in sorted(command_groups.items()):
        entry = _object_map(raw_entry, f"command group {command}")
        entry_profile = entry.get("entry_profile")
        if isinstance(entry_profile, str) and entry_profile in _OPTIONAL_PROFILES:
            expected_dispatcher_profiles[command] = (entry_profile, tuple(sorted(owner_roots[entry_profile])))
    actual_dispatcher_profiles = _dispatcher_profiles(repo_root)
    for command in sorted(set(expected_dispatcher_profiles) | set(actual_dispatcher_profiles)):
        expected = expected_dispatcher_profiles.get(command)
        actual = actual_dispatcher_profiles.get(command)
        if expected != actual:
            findings.append(Finding("DEP013", command, f"dispatcher profile {actual!r}; expected {expected!r}"))

    declared_roots = set(root_owners)
    for imported_module in sorted(_third_party_imports(repo_root)):
        owners = [root for root in declared_roots if imported_module == root or imported_module.startswith(f"{root}.")]
        if not owners:
            findings.append(Finding("DEP012", imported_module, "third-party import has no profile owner"))

    return ValidationReport(schema_version=1, findings=tuple(sorted(findings)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit the versioned report as JSON.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate(args.repo_root.resolve())
    except (OSError, ValueError, SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"dependency profile check: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print("Dependency profile check: PASS")
    else:
        print(f"Dependency profile check: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.rule_id} {finding.subject}: {finding.message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
