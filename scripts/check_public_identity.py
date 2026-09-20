#!/usr/bin/env python3
"""Validate public-candidate text against fieldkit's versioned identity policy."""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

if __package__:
    from scripts.json_policy import reject_duplicate_json_keys
else:
    from json_policy import reject_duplicate_json_keys

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path("docs/release-readiness/public-identity-policy.json")
_MAX_TEXT_BYTES = 2 * 1024 * 1024
_BINARY_SUFFIXES = frozenset({".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp", ".woff", ".woff2"})
_ALLOWANCE_CLASSIFICATIONS = frozenset(
    {
        "fictional_fixture",
        "integration_specific",
        "public_project_identity",
        "public_vendor_protocol",
        "release_cutover_policy",
    }
)


@dataclass(frozen=True)
class Rule:
    """One forbidden identity marker."""

    rule_id: str
    category: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Allowance:
    """A narrow, reviewed classification for one rule and exact path."""

    rule_id: str
    path: str
    pattern: re.Pattern[str]
    classification: str
    rationale: str


@dataclass(frozen=True, order=True)
class Finding:
    """A payload-safe public identity finding."""

    rule_id: str
    category: str
    path: str
    line: int


@dataclass(frozen=True)
class Document:
    """One explicit text subject and the repository path governing its allowances."""

    path: str
    policy_path: str
    text: str


@dataclass(frozen=True)
class ValidationReport:
    """Versioned output shared by human and JSON renderers."""

    schema_version: int
    scanned_files: int
    classified_matches: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "scanned_files": self.scanned_files,
            "classified_matches": self.classified_matches,
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True)
class Policy:
    """Validated identity policy input."""

    schema_version: int
    scope: tuple[str, ...]
    rules: tuple[Rule, ...]
    allowances: tuple[Allowance, ...]


def _object(value: object, subject: str) -> dict[str, object]:
    """Return a policy object whose keys are all strings."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _nonempty_string(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def _compile(value: object, subject: str) -> re.Pattern[str]:
    pattern = _nonempty_string(value, subject)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{subject} is not a valid regular expression") from exc


def parse_policy(data: bytes) -> Policy:
    """Strictly validate identity policy bytes from an already trusted source."""
    raw = _object(json.loads(data, object_pairs_hook=reject_duplicate_json_keys), str(POLICY_PATH))
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise ValueError(f"{POLICY_PATH}: schema_version must be 1")
    raw_scope = raw.get("scope")
    if (
        not isinstance(raw_scope, list)
        or not raw_scope
        or not all(isinstance(item, str) and item for item in raw_scope)
    ):
        raise ValueError(f"{POLICY_PATH}: scope must be a non-empty string list")
    scope = tuple(item for item in raw_scope if isinstance(item, str))

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(f"{POLICY_PATH}: rules must be a non-empty list")
    rules: list[Rule] = []
    for index, value in enumerate(raw_rules):
        entry = _object(value, f"rule {index}")
        rules.append(
            Rule(
                rule_id=_nonempty_string(entry.get("id"), f"rule {index} id"),
                category=_nonempty_string(entry.get("category"), f"rule {index} category"),
                pattern=_compile(entry.get("pattern"), f"rule {index} pattern"),
            )
        )
    rule_ids = [rule.rule_id for rule in rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise ValueError(f"{POLICY_PATH}: rule IDs must be unique")

    raw_allowances = raw.get("allowances")
    if not isinstance(raw_allowances, list):
        raise ValueError(f"{POLICY_PATH}: allowances must be a list")
    allowances: list[Allowance] = []
    for index, value in enumerate(raw_allowances):
        entry = _object(value, f"allowance {index}")
        rule_id = _nonempty_string(entry.get("rule_id"), f"allowance {index} rule_id")
        if rule_id not in rule_ids:
            raise ValueError(f"allowance {index} references unknown rule {rule_id!r}")
        raw_path = entry.get("path")
        raw_paths = entry.get("paths")
        if (raw_path is None) == (raw_paths is None):
            raise ValueError(f"allowance {index} must define exactly one of path or paths")
        if raw_path is not None:
            paths = [_nonempty_string(raw_path, f"allowance {index} path")]
        elif isinstance(raw_paths, list) and raw_paths and all(isinstance(path, str) and path for path in raw_paths):
            paths = [path for path in raw_paths if isinstance(path, str)]
        else:
            raise ValueError(f"allowance {index} paths must be a non-empty string list")
        for path in paths:
            if any(character in path for character in "*?[") or PurePosixPath(path).is_absolute():
                raise ValueError(f"allowance {index} path must be one exact repository-relative path")
        classification = _nonempty_string(entry.get("classification"), f"allowance {index} classification")
        if classification not in _ALLOWANCE_CLASSIFICATIONS:
            raise ValueError(f"allowance {index} has unsupported classification {classification!r}")
        pattern = _compile(entry.get("pattern"), f"allowance {index} pattern")
        rationale = _nonempty_string(entry.get("rationale"), f"allowance {index} rationale")
        allowances.extend(
            Allowance(
                rule_id=rule_id,
                path=path,
                pattern=pattern,
                classification=classification,
                rationale=rationale,
            )
            for path in paths
        )
    return Policy(schema_version=1, scope=scope, rules=tuple(rules), allowances=tuple(allowances))


def load_policy(repo_root: Path = REPO_ROOT) -> Policy:
    """Load and strictly validate the checked-in identity policy."""
    return parse_policy((repo_root / POLICY_PATH).read_bytes())


def _in_scope(path: str, scope: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise OSError("git ls-files failed")
    return tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def _decode_text(data: bytes, path: str) -> str | None:
    if PurePosixPath(path).suffix.lower() in _BINARY_SUFFIXES:
        return None
    if len(data) > _MAX_TEXT_BYTES:
        raise ValueError(f"cannot scan oversized in-scope text: {path}")
    if b"\0" in data:
        raise ValueError(f"cannot scan NUL-containing in-scope text: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot scan non-UTF-8 in-scope text: {path}") from exc


def _repository_documents(repo_root: Path, policy: Policy, tracked_paths: tuple[str, ...]) -> Iterable[Document]:
    for relative in tracked_paths:
        if not _in_scope(relative, policy.scope):
            continue
        try:
            data = (repo_root / relative).read_bytes()
        except FileNotFoundError:
            continue
        text = _decode_text(data, relative)
        if text is not None:
            yield Document(relative, relative, text)


def _artifact_documents(path: Path, policy: Policy) -> Iterable[Document]:
    prefix = f"artifact:{path.name}:"
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                text = _decode_text(archive.read(name), prefix + name)
                if text is not None:
                    is_metadata = name.endswith(".dist-info/METADATA")
                    policy_path = (
                        "pyproject.toml" if is_metadata else f"src/{name}" if name.startswith("fieldkit/") else name
                    )
                    if is_metadata or _in_scope(policy_path, policy.scope):
                        yield Document(prefix + name, policy_path, text)
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            for member in sorted(archive.getmembers(), key=lambda item: item.name):
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                text = _decode_text(stream.read(_MAX_TEXT_BYTES + 1), prefix + member.name)
                relative = member.name.split("/", maxsplit=1)[-1]
                is_metadata = relative == "PKG-INFO"
                policy_path = "pyproject.toml" if is_metadata else relative
                if text is not None and (is_metadata or _in_scope(policy_path, policy.scope)):
                    yield Document(prefix + member.name, policy_path, text)
        return
    raise ValueError(f"unsupported artifact type: {path.name}")


def scan_documents(
    policy: Policy,
    documents: Iterable[Document],
    known_policy_paths: frozenset[str],
) -> ValidationReport:
    """Apply identity policy to an explicit, caller-owned document set."""
    for allowance in policy.allowances:
        if allowance.path not in known_policy_paths:
            raise ValueError(
                f"{POLICY_PATH}: allowance {allowance.rule_id}/{allowance.path} is not a known policy path"
            )

    document_list = list(documents)

    allowances = {(allowance.rule_id, allowance.path): allowance for allowance in policy.allowances}
    if len(allowances) != len(policy.allowances):
        raise ValueError(f"{POLICY_PATH}: duplicate allowance rule/path pairs are not supported")

    findings: list[Finding] = []
    classified_matches = 0
    used_allowances: set[tuple[str, str]] = set()
    for document in document_list:
        for line_number, line in enumerate(document.text.splitlines(), start=1):
            for rule in policy.rules:
                matches = tuple(rule.pattern.finditer(line))
                if not matches:
                    continue
                allowance = allowances.get((rule.rule_id, document.policy_path))
                for match in matches:
                    classified = (
                        any(
                            allowed_match.start() <= match.start() and allowed_match.end() >= match.end()
                            for allowed_match in allowance.pattern.finditer(line)
                        )
                        if allowance is not None
                        else False
                    )
                    if classified:
                        classified_matches += 1
                        used_allowances.add((rule.rule_id, document.policy_path))
                    else:
                        findings.append(Finding(rule.rule_id, rule.category, document.path, line_number))
    unused_allowances = sorted(set(allowances) - used_allowances)
    if unused_allowances and not findings:
        formatted = ", ".join(f"{rule_id}/{path}" for rule_id, path in unused_allowances)
        raise ValueError(f"{POLICY_PATH}: unused allowance(s): {formatted}")

    return ValidationReport(
        schema_version=1,
        scanned_files=len(document_list),
        classified_matches=classified_matches,
        findings=tuple(sorted(set(findings))),
    )


def validate(repo_root: Path = REPO_ROOT, artifacts: tuple[Path, ...] = ()) -> ValidationReport:
    """Scan repository scope and optional built artifacts without exposing matched text."""
    policy = load_policy(repo_root)
    tracked_paths = _tracked_paths(repo_root)
    known_policy_paths = frozenset(path for path in tracked_paths if _in_scope(path, policy.scope))
    for allowance in policy.allowances:
        if allowance.path not in known_policy_paths:
            raise ValueError(
                f"{POLICY_PATH}: allowance {allowance.rule_id}/{allowance.path} is not a tracked in-scope path"
            )
    documents = list(_repository_documents(repo_root, policy, tracked_paths))
    for artifact in artifacts:
        documents.extend(_artifact_documents(artifact, policy))
    return scan_documents(policy, documents, known_policy_paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, action="append", default=[], help="Also scan a wheel or sdist.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit the versioned report as JSON.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate(args.repo_root.resolve(), tuple(path.resolve() for path in args.artifact))
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"Public identity check: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print(
            f"Public identity check: PASS ({report.scanned_files} file(s), "
            f"{report.classified_matches} classified match(es))"
        )
    else:
        print(f"Public identity check: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.rule_id} {finding.category} {finding.path}:{finding.line}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
