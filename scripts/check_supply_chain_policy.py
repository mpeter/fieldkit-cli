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
from importlib import metadata
from pathlib import Path

import _supply_chain_policy as policy_check

_PINNED_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[A-Za-z0-9_.!+-]+)(?:\s*;.*)?(?:\s+\\)?$")

_LICENSE_FIELD_EXPRESSIONS = {
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "mit": "MIT",
    "mit license": "MIT",
    "apache-2.0 and mit": "Apache-2.0 AND MIT",
    "apache-2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause or apache-2.0": "BSD-3-Clause OR Apache-2.0",
    "mit or apache-2.0": "MIT OR Apache-2.0",
    "mpl-2.0 and mit": "MPL-2.0 AND MIT",
}

_MIT_LICENSE_SIGNATURE = "permission is hereby granted, free of charge"
_APACHE_LICENSE_SIGNATURE = "licensed under the apache license, version 2.0"
_BSD_3_CLAUSE_SIGNATURE = "redistribution and use in source and binary forms"
_BSD_3_CLAUSE_NAME_CLAUSE = "neither the name of the"

_LICENSE_CLASSIFIER_EXPRESSIONS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


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


def _license_expression(distribution: metadata.Distribution) -> str:
    """Return one deterministic SPDX expression from installed package metadata.

    PEP 639 metadata is authoritative. Older distributions commonly expose a
    normalized License field or exactly one Trove classifier; ambiguous or
    unrecognized metadata deliberately remains UNKNOWN for policy review.
    """
    headers = distribution.metadata.json
    expression = headers.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        return expression
    license_field = headers.get("license")
    if isinstance(license_field, str):
        normalized = " ".join(license_field.casefold().split())
        if normalized in _LICENSE_FIELD_EXPRESSIONS:
            return _LICENSE_FIELD_EXPRESSIONS[normalized]
        if normalized.startswith("mit license ") and _MIT_LICENSE_SIGNATURE in normalized:
            return "MIT"
    get_all = getattr(distribution.metadata, "get_all", None)
    classifiers = (get_all("Classifier") or []) if callable(get_all) else []
    mapped = {
        _LICENSE_CLASSIFIER_EXPRESSIONS[classifier]
        for classifier in classifiers
        if classifier in _LICENSE_CLASSIFIER_EXPRESSIONS
    }
    if len(mapped) == 1:
        return mapped.pop()
    declared_license_files = (get_all("License-File") or []) if callable(get_all) else []
    declared_license_paths = {
        declared_path.replace("\\", "/").lstrip("/")
        for declared_path in declared_license_files
        if isinstance(declared_path, str) and declared_path.strip()
    }
    detected_licenses: set[str] = set()
    for file in distribution.files or ():
        package_path = str(file).replace("\\", "/")
        if not any(
            package_path == declared_path or package_path.endswith(f"/{declared_path}")
            for declared_path in declared_license_paths
        ):
            continue
        path = distribution.locate_file(file)
        if not path.is_file():
            continue
        try:
            contents = path.read_text(encoding="utf-8").casefold()
        except OSError:
            continue
        if _APACHE_LICENSE_SIGNATURE in contents:
            detected_licenses.add("Apache-2.0")
        if _BSD_3_CLAUSE_SIGNATURE in contents and _BSD_3_CLAUSE_NAME_CLAUSE in contents:
            detected_licenses.add("BSD-3-Clause")
        if _MIT_LICENSE_SIGNATURE in contents:
            detected_licenses.add("MIT")
    if detected_licenses:
        return " AND ".join(sorted(detected_licenses))
    return "UNKNOWN"


def _resolved_package_metadata() -> list[dict[str, str]]:
    """Collect the installed distributions that the locked candidate environment resolved."""
    packages: list[dict[str, str]] = []
    observed_packages: set[tuple[str, str, str]] = set()
    for distribution in metadata.distributions():
        name = distribution.metadata.json.get("name")
        version = distribution.version
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            raise ValueError("installed distribution metadata must include a name and version")
        license_expression = _license_expression(distribution)
        package = (name, version, license_expression)
        if package in observed_packages:
            continue
        observed_packages.add(package)
        packages.append(
            {
                "name": name,
                "version": version,
                "license_expression": license_expression,
            }
        )
    return packages


def _sbom_package_urls(path: Path) -> tuple[tuple[str, ...], str]:
    """Read exact PyPI component identities from uv's locked CycloneDX output."""
    contents = path.read_bytes()
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
        if not isinstance(package_url, str):
            raise ValueError(f"locked SBOM component {index} purl must be a string")
        package_urls.append(package_url)
    return tuple(package_urls), hashlib.sha256(contents).hexdigest()


def _locked_requirements_package_urls(path: Path) -> tuple[str, ...]:
    """Read exact package URLs from uv's lock-derived requirements export."""
    package_urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        match = _PINNED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise ValueError("platform requirements must pin every package to exactly one version")
        package_urls.append(policy_check.package_url(match["name"], match["version"]))
    if len(package_urls) != len(set(package_urls)):
        raise ValueError("platform requirements must contain unique package URLs")
    return tuple(sorted(package_urls))


def _run_license_evidence(args: argparse.Namespace) -> int:
    policy = policy_check.load_policy(args.policy)
    _, sbom_sha256 = _sbom_package_urls(args.sbom)
    expected_package_urls = _locked_requirements_package_urls(args.platform_requirements)
    evidence = policy_check.build_license_evidence(
        _resolved_package_metadata(),
        policy,
        scope=args.scope,
        revision=args.revision,
        export_policy_sha256=args.export_policy_sha256,
        sbom_sha256=sbom_sha256,
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
