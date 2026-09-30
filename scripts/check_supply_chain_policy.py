#!/usr/bin/env python3
"""Validate dependency policy and normalize supply-chain scanner evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path

if __package__ in {None, ""}:
    scripts_spec = ModuleSpec("scripts", loader=None, is_package=True)
    scripts_spec.submodule_search_locations = [str(Path(__file__).resolve().parent)]
    sys.modules["scripts"] = module_from_spec(scripts_spec)

from scripts import _supply_chain_policy as policy_check
from scripts import runtime_license_inventory as inventory

_EXACT_PYPI_PURL = re.compile(r"^pkg:pypi/(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<version>[A-Za-z0-9_.!+-]+)$")
_UNPROJECTABLE_MARKER = re.compile(r"\b(?:extra|extras|dependency_groups)\b")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    policy = subparsers.add_parser("policy")
    policy.add_argument("--repo-root", type=Path, default=policy_check.REPO_ROOT)
    review = subparsers.add_parser("dependency-review")
    review.add_argument("--policy", type=Path, default=policy_check.REPO_ROOT / policy_check.POLICY_PATH)
    changes = review.add_mutually_exclusive_group(required=True)
    changes.add_argument("--changes", type=Path)
    changes.add_argument("--changes-env")
    review.add_argument("--output", type=Path)
    review.add_argument("--upstream-outcome", default="success")
    review.add_argument("--today", type=date.fromisoformat)
    audit = subparsers.add_parser("audit-report")
    audit.add_argument("--policy", type=Path, default=policy_check.REPO_ROOT / policy_check.POLICY_PATH)
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--scope", choices=("runtime-all-extras", "development"), required=True)
    audit.add_argument("--revision", required=True)
    audit.add_argument("--tool-version", required=True)
    audit.add_argument("--scanner-exit-code", type=int, required=True)
    licenses = subparsers.add_parser("license-evidence")
    licenses.add_argument("--policy", type=Path, required=True)
    licenses.add_argument("--output", type=Path, required=True)
    licenses.add_argument(
        "--scope", choices=("locked-all-groups-all-extras", "runtime-all-extras", "runtime-default"), required=True
    )
    licenses.add_argument("--revision", required=True)
    licenses.add_argument("--export-policy-sha256", required=True)
    licenses.add_argument("--sbom", type=Path, required=True)
    licenses.add_argument("--platform-requirements", type=Path, required=True)
    licenses.add_argument("--observations", type=Path, required=True)
    licenses.add_argument("--observations-sha256", required=True)
    return parser


def _run_policy(args: argparse.Namespace) -> int:
    report = policy_check.validate_repository(args.repo_root.resolve())
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.ok else 1


def _run_dependency_review(args: argparse.Namespace) -> int:
    policy = policy_check.load_policy(args.policy, today=args.today)
    changes_text = (
        args.changes.read_text(encoding="utf-8") if args.changes is not None else os.environ.get(args.changes_env, "")
    )
    changes_payload = json.loads(changes_text)
    report = policy_check.review_dependency_changes(
        changes_payload,
        policy,
        today=args.today,
        upstream_outcome=args.upstream_outcome,
    )
    if args.output is not None:
        _write_json(args.output, report.to_dict())
    else:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.ok else 1


def _run_audit_report(args: argparse.Namespace) -> int:
    policy = policy_check.load_policy(args.policy)
    raw = {} if args.scanner_exit_code not in {0, 1} else json.loads(args.input.read_text(encoding="utf-8"))
    evidence = policy_check.build_audit_evidence(
        raw,
        policy,
        scope=args.scope,
        revision=args.revision,
        tool_version=args.tool_version,
        scanner_exit_code=args.scanner_exit_code,
    )
    _write_json(args.output, evidence.to_dict())
    return 0 if evidence.status == "pass" else 1


def _sbom_package_urls(path: Path) -> tuple[tuple[str, ...], str]:
    """Read exact PyPI component identities from uv's locked CycloneDX output."""
    contents = inventory.read_snapshot(path)
    document = json.loads(contents)
    if not isinstance(document, dict):
        raise ValueError("locked SBOM must be an object")
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("locked SBOM components must be a list")
    package_urls: list[str] = []
    for index, value in enumerate(components):
        if not isinstance(value, dict) or set(value).isdisjoint({"purl"}):
            raise ValueError(f"locked SBOM component {index} must contain a purl")
        package_url = value["purl"]
        if not isinstance(package_url, str) or _EXACT_PYPI_PURL.fullmatch(package_url) is None:
            raise ValueError(f"locked SBOM component {index} must contain a canonical exact-version PyPI purl")
        package_urls.append(package_url)
    if not package_urls or len(package_urls) != len(set(package_urls)):
        raise ValueError("locked SBOM must contain a nonempty unique package inventory")
    return tuple(sorted(package_urls)), hashlib.sha256(contents).hexdigest()


def _requirements_package_urls(contents: str, *, target_environment: dict[str, str] | None) -> tuple[str, ...]:
    """Project one requirements snapshot without rereading a mutable export."""
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError as exc:
        raise ValueError("license evidence requires packaging in the QA environment") from exc

    package_urls: list[str] = []
    target_names: set[str] = set()
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0].isspace() and re.fullmatch(r"--hash=sha256:[a-f0-9]{64}(?:\s+\\)?", stripped):
            continue
        requirement = Requirement(stripped.removesuffix("\\").rstrip())
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError("platform requirements must pin every package to exactly one version")
        version = specifiers[0].version
        if str(Version(version)) != version:
            raise ValueError("platform requirements must use canonical package versions")
        if requirement.marker is not None:
            if _UNPROJECTABLE_MARKER.search(str(requirement.marker)):
                raise ValueError("platform requirements must resolve extras and dependency groups before export")
            if target_environment is not None and not requirement.marker.evaluate(environment=target_environment):
                continue
        package_url = inventory.package_url(requirement.name, version)
        normalized_name = package_url.removeprefix("pkg:pypi/").split("@", 1)[0]
        if target_environment is not None and normalized_name in target_names:
            raise ValueError("platform requirements select duplicate or ambiguous target package versions")
        target_names.add(normalized_name)
        package_urls.append(package_url)
    if len(package_urls) != len(set(package_urls)):
        raise ValueError("platform requirements must contain unique package URLs")
    return tuple(sorted(package_urls))


def _run_license_evidence(args: argparse.Namespace) -> int:
    policy = policy_check.load_policy(args.policy)
    observation_contents = inventory.read_snapshot(args.observations)
    observation_digest = hashlib.sha256(observation_contents).hexdigest()
    if observation_digest != args.observations_sha256:
        raise ValueError("runtime license observations do not match their captured digest")
    packages, environment = inventory.parse_observations(observation_contents)
    sbom_package_urls, sbom_sha256 = _sbom_package_urls(args.sbom)
    requirements_bytes = inventory.read_snapshot(args.platform_requirements)
    requirements_contents = requirements_bytes.decode("utf-8")
    union_package_urls = _requirements_package_urls(requirements_contents, target_environment=None)
    if sbom_package_urls != union_package_urls:
        raise ValueError("locked SBOM inventory must exactly match the cross-platform requirements inventory")
    expected_package_urls = _requirements_package_urls(requirements_contents, target_environment=environment)
    evidence = policy_check.build_license_evidence(
        packages,
        policy,
        scope=args.scope,
        revision=args.revision,
        export_policy_sha256=args.export_policy_sha256,
        sbom_sha256=sbom_sha256,
        observations_sha256=observation_digest,
        platform_requirements_sha256=hashlib.sha256(requirements_bytes).hexdigest(),
        marker_environment=environment,
        expected_package_urls=expected_package_urls,
    )
    _write_json(args.output, evidence.to_dict())
    return 0 if evidence.status == "pass" else 1


def _write_error_evidence(args: argparse.Namespace, exc: Exception) -> None:
    output_path = getattr(args, "output", None)
    if not isinstance(output_path, Path):
        return
    if args.command == "audit-report":
        _write_json(
            output_path,
            {
                "schema_version": 1,
                "status": "error",
                "scope": args.scope,
                "revision": args.revision,
                "tool_version": args.tool_version,
                "scanner_exit_code": args.scanner_exit_code,
                "error": str(exc),
            },
        )
    elif args.command == "dependency-review":
        _write_json(
            output_path,
            {
                "schema_version": 1,
                "status": "error",
                "reviewed_changes": 0,
                "findings": [],
                "error": str(exc),
            },
        )
    elif args.command == "license-evidence":
        _write_json(
            output_path,
            {
                "schema_version": 1,
                "status": "error",
                "scope": args.scope,
                "revision": args.revision,
                "export_policy_sha256": args.export_policy_sha256,
                "sbom": str(args.sbom),
                "error": str(exc),
            },
        )


def main(argv: list[str] | None = None) -> int:
    """Run one supply-chain policy operation with stable process statuses."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "policy":
            return _run_policy(args)
        if args.command == "dependency-review":
            return _run_dependency_review(args)
        if args.command == "license-evidence":
            return _run_license_evidence(args)
        return _run_audit_report(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _write_error_evidence(args, exc)
        print(f"Supply-chain policy: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
