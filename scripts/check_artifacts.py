#!/usr/bin/env python3
"""Validate fieldkit wheel and source-distribution contents and metadata."""

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path("docs/release-readiness/artifact-policy.json")
PYPROJECT_PATH = Path("pyproject.toml")
_MAX_DIAGNOSTICS = 20


@dataclass(frozen=True)
class RequiredFamily:
    """One source asset family that every release artifact must contain."""

    criterion_id: str
    name: str
    source_globs: tuple[str, ...]


@dataclass(frozen=True)
class ForbiddenPath:
    """One payload-safe forbidden archive-path rule."""

    criterion_id: str
    glob: str
    category: str


@dataclass(frozen=True)
class TrackedPayload:
    """Exact tracked-source boundary for installable package payloads."""

    criterion_id: str
    source_root: str


@dataclass(frozen=True)
class Policy:
    """Validated release artifact policy."""

    required_families: tuple[RequiredFamily, ...]
    wheel_allowed: tuple[str, ...]
    sdist_allowed: tuple[str, ...]
    forbidden_paths: tuple[ForbiddenPath, ...]
    tracked_payload: TrackedPayload


@dataclass(frozen=True, order=True)
class CriterionResult:
    """Outcome of one stable artifact-policy criterion."""

    criterion_id: str
    status: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactResult:
    """Digest and policy outcomes for one artifact."""

    name: str
    kind: str
    sha256: str
    criteria: tuple[CriterionResult, ...]

    @property
    def ok(self) -> bool:
        """Return whether every criterion passed."""
        return all(criterion.status == "pass" for criterion in self.criteria)


@dataclass(frozen=True)
class ValidationReport:
    """Versioned evidence for artifacts built from one source revision."""

    schema_version: int
    source_revision: str
    artifacts: tuple[ArtifactResult, ...]

    @property
    def ok(self) -> bool:
        """Return whether at least one artifact was supplied and all passed."""
        return bool(self.artifacts) and all(artifact.ok for artifact in self.artifacts)

    def to_dict(self) -> dict[str, object]:
        """Render the stable machine-readable report shape."""
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "source_revision": self.source_revision,
            "artifacts": [
                {
                    **asdict(artifact),
                    "status": "pass" if artifact.ok else "fail",
                }
                for artifact in self.artifacts
            ],
        }


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON/TOML object with string keys."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _strings(value: object, subject: str) -> tuple[str, ...]:
    """Require a non-empty list of non-empty strings."""
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{subject} must be a non-empty string list")
    return tuple(item for item in value if isinstance(item, str))


def _string(value: object, subject: str) -> str:
    """Require one non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def load_policy(repo_root: Path = REPO_ROOT) -> Policy:
    """Load and strictly validate the checked-in artifact policy."""
    raw = _object(json.loads((repo_root / POLICY_PATH).read_text(encoding="utf-8")), str(POLICY_PATH))
    if raw.get("schema_version") != 1:
        raise ValueError(f"{POLICY_PATH}: schema_version must be 1")
    raw_families = raw.get("required_families")
    if not isinstance(raw_families, list) or not raw_families:
        raise ValueError(f"{POLICY_PATH}: required_families must be a non-empty list")
    families = tuple(
        RequiredFamily(
            _string(entry.get("id"), f"required family {index} id"),
            _string(entry.get("name"), f"required family {index} name"),
            _strings(entry.get("source_globs"), f"required family {index} source_globs"),
        )
        for index, value in enumerate(raw_families)
        for entry in (_object(value, f"required family {index}"),)
    )
    allowed = _object(raw.get("allowed_paths"), "allowed_paths")
    raw_forbidden = raw.get("forbidden_paths")
    if not isinstance(raw_forbidden, list) or not raw_forbidden:
        raise ValueError(f"{POLICY_PATH}: forbidden_paths must be a non-empty list")
    forbidden = tuple(
        ForbiddenPath(
            _string(entry.get("id"), f"forbidden path {index} id"),
            _string(entry.get("glob"), f"forbidden path {index} glob"),
            _string(entry.get("category"), f"forbidden path {index} category"),
        )
        for index, value in enumerate(raw_forbidden)
        for entry in (_object(value, f"forbidden path {index}"),)
    )
    tracked = _object(raw.get("tracked_payload"), "tracked_payload")
    tracked_payload = TrackedPayload(
        _string(tracked.get("id"), "tracked_payload.id"),
        _string(tracked.get("source_root"), "tracked_payload.source_root").rstrip("/"),
    )
    ids = (
        [family.criterion_id for family in families]
        + [rule.criterion_id for rule in forbidden]
        + [tracked_payload.criterion_id]
    )
    if len(ids) != len(set(ids)):
        raise ValueError(f"{POLICY_PATH}: criterion IDs must be unique")
    return Policy(
        families,
        _strings(allowed.get("wheel"), "allowed_paths.wheel"),
        _strings(allowed.get("sdist"), "allowed_paths.sdist"),
        forbidden,
        tracked_payload,
    )


def _source_revision(repo_root: Path) -> str:
    """Read the exact Git revision represented by the evidence."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise OSError("git rev-parse HEAD failed")
    return result.stdout.strip()


def _artifact_entries(path: Path) -> tuple[str, tuple[str, ...], bytes]:
    """Return normalized member names and metadata from a wheel or sdist."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = tuple(sorted(name for name in archive.namelist() if not name.endswith("/")))
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ValueError(f"{path.name}: expected exactly one METADATA file")
            return "wheel", names, archive.read(metadata_names[0])
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            members = tuple(sorted(member.name for member in archive.getmembers() if member.isfile()))
            roots = {name.split("/", maxsplit=1)[0] for name in members}
            if len(roots) != 1:
                raise ValueError(f"{path.name}: expected exactly one source-distribution root")
            root = next(iter(roots))
            names = tuple(name.removeprefix(f"{root}/") for name in members)
            member = archive.getmember(f"{root}/PKG-INFO")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"{path.name}: PKG-INFO is unreadable")
            return "sdist", names, stream.read()
    raise ValueError(f"unsupported artifact type: {path.name}")


def _criterion(criterion_id: str, diagnostics: list[str]) -> CriterionResult:
    """Build a criterion result with sorted, bounded diagnostics."""
    bounded = tuple(sorted(set(diagnostics))[:_MAX_DIAGNOSTICS])
    omitted = len(set(diagnostics)) - len(bounded)
    if omitted > 0:
        bounded += (f"... {omitted} additional finding(s) omitted",)
    return CriterionResult(criterion_id, "fail" if diagnostics else "pass", bounded)


def _matches(path: str, pattern: str) -> bool:
    """Match globstar patterns at both root and nested boundaries."""
    variants = {pattern, pattern.replace("/**/", "/")}
    if pattern.startswith("**/"):
        variants.add(pattern.removeprefix("**/"))
    return any(fnmatch.fnmatchcase(path, variant) for variant in variants)


def _expected_paths(
    repo_root: Path,
    family: RequiredFamily,
    kind: str,
    tracked_paths: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Map tracked source assets to their expected archive paths."""
    if tracked_paths is None:
        result = subprocess.run(
            ["git", "ls-files", "-z", "src/fieldkit"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise OSError("git ls-files src/fieldkit failed")
        tracked = tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)
    else:
        tracked = tracked_paths
    source_paths = {path for path in tracked if any(_matches(path, pattern) for pattern in family.source_globs)}
    if not source_paths:
        raise ValueError(f"{POLICY_PATH}: {family.name} matches no source files")
    if kind == "wheel":
        return tuple(sorted(path.removeprefix("src/") for path in source_paths))
    return tuple(sorted(source_paths))


def _tracked_package_paths(
    repo_root: Path,
    tracked_payload: TrackedPayload,
    kind: str,
    tracked_paths: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Map every tracked package source to its only permitted artifact path."""
    if tracked_paths is None:
        result = subprocess.run(
            ["git", "ls-files", "-z", tracked_payload.source_root],
            cwd=repo_root,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise OSError(f"git ls-files {tracked_payload.source_root} failed")
        tracked = tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)
    else:
        prefix = f"{tracked_payload.source_root}/"
        tracked = tuple(path for path in tracked_paths if path.startswith(prefix))
    if not tracked:
        raise ValueError(f"{POLICY_PATH}: tracked_payload matches no source files")
    if kind == "wheel":
        return tuple(sorted(path.removeprefix("src/") for path in tracked))
    return tuple(sorted(tracked))


def _metadata_result(metadata_bytes: bytes, project: dict[str, object]) -> CriterionResult:
    """Compare generated package metadata with declared project identity."""
    metadata = BytesParser().parsebytes(metadata_bytes)
    legacy_metadata_versions = {"1.0", "1.1", "1.2", "2.0", "2.1", "2.2", "2.3"}
    license_field = "License" if metadata.get("Metadata-Version") in legacy_metadata_versions else "License-Expression"
    expected = {
        "Name": project.get("name"),
        "Version": project.get("version"),
        license_field: project.get("license"),
        "Description-Content-Type": "text/markdown",
    }
    diagnostics = [
        f"{key}: expected {value!r}, found {metadata.get(key)!r}"
        for key, value in expected.items()
        if metadata.get(key) != value
    ]
    payload = metadata.get_payload()
    if not isinstance(payload, str) or not payload.strip():
        diagnostics.append("README payload is empty")
    return _criterion("ART001", diagnostics)


def validate_artifact(
    path: Path,
    repo_root: Path,
    policy: Policy,
    project: dict[str, object],
    *,
    tracked_payload_paths: tuple[str, ...] | None = None,
) -> ArtifactResult:
    """Validate one artifact without exposing member contents."""
    kind, names, metadata = _artifact_entries(path)
    name_set = set(names)
    criteria = [_metadata_result(metadata, project)]
    for family in policy.required_families:
        missing = [
            f"missing {name}"
            for name in _expected_paths(repo_root, family, kind, tracked_payload_paths)
            if name not in name_set
        ]
        criteria.append(_criterion(family.criterion_id, missing))
    tracked_paths = set(_tracked_package_paths(repo_root, policy.tracked_payload, kind, tracked_payload_paths))
    package_prefix = "fieldkit/" if kind == "wheel" else f"{policy.tracked_payload.source_root}/"
    criteria.append(
        _criterion(
            policy.tracked_payload.criterion_id,
            [
                f"untracked package path {name}"
                for name in names
                if name.startswith(package_prefix) and name not in tracked_paths
            ],
        )
    )
    allowed = policy.wheel_allowed if kind == "wheel" else policy.sdist_allowed
    criteria.append(
        _criterion(
            "ART200",
            [f"unexpected path {name}" for name in names if not any(_matches(name, pattern) for pattern in allowed)],
        )
    )
    for rule in policy.forbidden_paths:
        criteria.append(
            _criterion(rule.criterion_id, [f"{rule.category}: {name}" for name in names if _matches(name, rule.glob)])
        )
    return ArtifactResult(path.name, kind, hashlib.sha256(path.read_bytes()).hexdigest(), tuple(criteria))


def validate(
    artifacts: tuple[Path, ...],
    repo_root: Path = REPO_ROOT,
    *,
    source_revision: str | None = None,
    tracked_payload_paths: tuple[str, ...] | None = None,
) -> ValidationReport:
    """Validate one artifact per kind and bind results to the current revision."""
    if not artifacts:
        raise ValueError("at least one artifact is required")
    policy = load_policy(repo_root)
    pyproject = tomllib.loads((repo_root / PYPROJECT_PATH).read_text(encoding="utf-8"))
    project = _object(pyproject.get("project"), "pyproject project")
    results = tuple(
        validate_artifact(path, repo_root, policy, project, tracked_payload_paths=tracked_payload_paths)
        for path in artifacts
    )
    kinds = [result.kind for result in results]
    if len(kinds) != len(set(kinds)):
        raise ValueError("provide at most one artifact of each kind")
    revision = source_revision if source_revision is not None else _source_revision(repo_root)
    if not revision:
        raise ValueError("source_revision must be non-empty")
    return ValidationReport(1, revision, results)


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run artifact validation and return a stable process status."""
    args = _parser().parse_args(argv)
    try:
        report = validate(tuple(path.resolve() for path in args.artifacts), args.repo_root.resolve())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        tarfile.TarError,
        tomllib.TOMLDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"Artifact check: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Artifact check: {'PASS' if report.ok else 'FAIL'} ({report.source_revision})")
        for artifact in report.artifacts:
            print(f"  {artifact.kind}: {artifact.name} sha256:{artifact.sha256}")
            for criterion in artifact.criteria:
                print(f"    {criterion.criterion_id}: {criterion.status.upper()}")
                for diagnostic in criterion.diagnostics:
                    print(f"      {diagnostic}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
