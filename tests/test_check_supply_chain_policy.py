"""Failure-capable contracts for dependency and scanner policy."""

import json
import shutil
from datetime import date
from pathlib import Path

import _supply_chain_policy as checker
import check_supply_chain_policy as cli_checker
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOVERNED_ACTION_CASES = (
    (
        "CODEQL_WORKFLOW_PATH",
        "github/codeql-action/init",
        "cdf488f595d80d6e07e03d4674febd5ab45fa938",
        "DEP203",
    ),
    (
        "CODEQL_WORKFLOW_PATH",
        "github/codeql-action/analyze",
        "cdf488f595d80d6e07e03d4674febd5ab45fa938",
        "DEP203",
    ),
    (
        "SCORECARD_WORKFLOW_PATH",
        "ossf/scorecard-action",
        "2d1146689b8cda280b9bc96326124645441f03bc",
        "DEP204",
    ),
)


@pytest.fixture
def repository_policy_repo(tmp_path: Path) -> Path:
    for relative in checker.REPOSITORY_POLICY_PATHS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    return tmp_path


def _policy(tmp_path: Path, *, expires_on: str = "2026-12-10") -> Path:
    path = tmp_path / "dependency-policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vulnerability_threshold": "low",
                "allowed_spdx_licenses": ["Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MIT"],
                "unknown_license_policy": "fail-unless-excepted",
                "license_exceptions": [
                    {
                        "package_url": "pkg:pypi/example-native@1.2.3",
                        "spdx_license": "Apache-2.0",
                        "owner": "@maintainer",
                        "evidence_url": "https://example.com/example-native/LICENSE",
                        "rationale": "Installed metadata omits the license expression.",
                        "expires_on": expires_on,
                        "review_condition": "Review on any package version or metadata change.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_live_dependency_policy_passes() -> None:
    """The checked-in policy remains strict, complete, and internally consistent."""
    report = checker.validate_repository(_REPO_ROOT, today=date(2026, 9, 11))
    policy = checker.load_policy(_REPO_ROOT / checker.POLICY_PATH, today=date(2026, 9, 11))

    assert report.ok
    assert policy.vulnerability_threshold == "low"
    assert policy.unknown_license_policy == "fail-unless-excepted"


def test_repository_policy_detects_weakened_vulnerability_threshold(repository_policy_repo: Path) -> None:
    """Workflow settings cannot silently drift below the checked-in policy."""
    workflow = repository_policy_repo / checker.DEPENDENCY_REVIEW_WORKFLOW_PATH
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace("fail-on-severity: low", "fail-on-severity: critical"),
        encoding="utf-8",
    )

    report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not report.ok
    assert any(finding.criterion_id == "DEP201" for finding in report.findings)


@pytest.mark.parametrize(
    ("path_name", "action_name", "approved_pin", "criterion_id"),
    _GOVERNED_ACTION_CASES,
)
def test_repository_policy_accepts_only_approved_analysis_pins(
    repository_policy_repo: Path,
    path_name: str,
    action_name: str,
    approved_pin: str,
    criterion_id: str,
) -> None:
    """Comments cannot disguise an arbitrary executable action revision."""
    approved_report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert approved_report.ok

    workflow_path = repository_policy_repo / getattr(checker, path_name)
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(f"{action_name}@{approved_pin}", f"{action_name}@{'a' * 40}")
        + f"\n# {action_name}@{approved_pin}\n",
        encoding="utf-8",
    )

    replacement_report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not replacement_report.ok
    assert any(finding.criterion_id == criterion_id for finding in replacement_report.findings)


@pytest.mark.parametrize(
    ("path_name", "action_name", "approved_pin", "criterion_id"),
    _GOVERNED_ACTION_CASES,
)
@pytest.mark.parametrize(
    "extra_step",
    [
        "      - name: Unapproved duplicate\n        uses: {reference}",
        '      - name: Quoted unapproved duplicate\n        uses: "{reference}"',
        '      - {{uses: "{reference}"}}',
    ],
)
def test_repository_policy_rejects_additional_unapproved_action_pin(
    repository_policy_repo: Path,
    path_name: str,
    action_name: str,
    approved_pin: str,
    criterion_id: str,
    extra_step: str,
) -> None:
    """Every executable use of a governed action must retain the approved revision."""
    workflow_path = repository_policy_repo / getattr(checker, path_name)
    approved_reference = f"{action_name}@{approved_pin}"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            f"- uses: {approved_reference}",
            f"- uses: {approved_reference}\n" + extra_step.format(reference=f"{action_name}@{'a' * 40}"),
        ),
        encoding="utf-8",
    )

    report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not report.ok
    assert any(finding.criterion_id == criterion_id for finding in report.findings)


@pytest.mark.parametrize(
    ("path_name", "action_name", "approved_pin", "criterion_id"),
    _GOVERNED_ACTION_CASES,
)
def test_repository_policy_rejects_case_variant_unapproved_action_pin(
    repository_policy_repo: Path,
    path_name: str,
    action_name: str,
    approved_pin: str,
    criterion_id: str,
) -> None:
    """Action owner and repository casing cannot bypass revision enforcement."""
    workflow_path = repository_policy_repo / getattr(checker, path_name)
    approved_reference = f"{action_name}@{approved_pin}"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            f"- uses: {approved_reference}",
            f"- uses: {approved_reference}\n      - uses: {action_name.upper()}@{'a' * 40}",
        ),
        encoding="utf-8",
    )

    report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not report.ok
    assert any(finding.criterion_id == criterion_id for finding in report.findings)


def test_repository_policy_rejects_recursive_workflow_alias(repository_policy_repo: Path) -> None:
    """Recursive YAML aliases fail closed instead of trapping policy validation."""
    workflow_path = repository_policy_repo / checker.SCORECARD_WORKFLOW_PATH
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8") + "\nrecursive: &recursive\n  self: *recursive\n",
        encoding="utf-8",
    )

    report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not report.ok
    assert any(finding.criterion_id == "DEP204" for finding in report.findings)


@pytest.mark.parametrize(
    ("path_name", "needle", "replacement", "criterion_id"),
    [
        ("CODEQL_WORKFLOW_PATH", "language: [python, actions]", "language: [python]", "DEP203"),
        ("SCORECARD_WORKFLOW_PATH", "publish_results: true", "publish_results: false", "DEP204"),
    ],
)
def test_repository_policy_rejects_disabled_analysis_paths(
    repository_policy_repo: Path,
    path_name: str,
    needle: str,
    replacement: str,
    criterion_id: str,
) -> None:
    """A workflow edit cannot quietly remove a language or public Scorecard publication."""
    workflow_path = repository_policy_repo / getattr(checker, path_name)
    workflow_path.write_text(workflow_path.read_text(encoding="utf-8").replace(needle, replacement), encoding="utf-8")

    report = checker.validate_repository(repository_policy_repo, today=date(2026, 9, 11))

    assert not report.ok
    assert any(finding.criterion_id == criterion_id for finding in report.findings)


def test_unknown_license_without_exception_fails(tmp_path: Path) -> None:
    """Missing metadata must not be silently accepted by the upstream action."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))
    changes = [
        {
            "change_type": "added",
            "manifest": "uv.lock",
            "ecosystem": "pip",
            "name": "unclassified",
            "version": "4.5.6",
            "package_url": "pkg:pypi/unclassified@4.5.6",
            "license": None,
        }
    ]

    report = checker.review_dependency_changes(changes, policy, today=date(2026, 9, 11))

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP102"
    assert report.findings[0].subject == "pkg:pypi/unclassified@4.5.6"


def test_exact_unexpired_unknown_license_exception_passes(tmp_path: Path) -> None:
    """A factual, version-bounded exception permits only its reviewed package."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))
    changes = [
        {
            "change_type": "added",
            "manifest": "uv.lock",
            "ecosystem": "pip",
            "name": "example-native",
            "version": "1.2.3",
            "package_url": "pkg:pypi/example-native@1.2.3",
            "license": "UNKNOWN",
        }
    ]

    report = checker.review_dependency_changes(changes, policy, today=date(2026, 9, 11))

    assert report.ok
    assert report.findings == ()


def test_expired_unknown_license_exception_fails_closed(tmp_path: Path) -> None:
    """An exception cannot survive its explicit review deadline."""
    with pytest.raises(ValueError, match="expired license exception"):
        checker.load_policy(_policy(tmp_path, expires_on="2026-09-10"), today=date(2026, 9, 11))


def test_locked_graph_license_evidence_records_every_observed_package(tmp_path: Path) -> None:
    """A resolved candidate graph is evaluated package-by-package, not by PR delta."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    evidence = checker.build_license_evidence(
        [
            {"name": "Example-Base", "version": "2.0.0", "license_expression": "MIT"},
            {"name": "example-native", "version": "1.2.3", "license_expression": "UNKNOWN"},
        ],
        policy,
        scope="locked-all-groups-all-extras",
        revision="d" * 40,
        export_policy_sha256="e" * 64,
        sbom_sha256="f" * 64,
        expected_package_urls=("pkg:pypi/example-base@2.0.0", "pkg:pypi/example-native@1.2.3"),
        today=date(2026, 9, 11),
    )

    assert evidence.status == "pass"
    assert evidence.observed_packages == 2
    assert [(item.package_url, item.license_expression) for item in evidence.packages] == [
        ("pkg:pypi/example-base@2.0.0", "MIT"),
        ("pkg:pypi/example-native@1.2.3", "UNKNOWN"),
    ]
    assert evidence.export_policy_sha256 == "e" * 64


def test_locked_graph_license_evidence_rejects_unknown_metadata_without_exact_exception(tmp_path: Path) -> None:
    """A clean resolved environment cannot hide a package without usable license metadata."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    evidence = checker.build_license_evidence(
        [{"name": "unclassified", "version": "4.5.6", "license_expression": "UNKNOWN"}],
        policy,
        scope="locked-all-groups-all-extras",
        revision="d" * 40,
        export_policy_sha256="e" * 64,
        sbom_sha256="f" * 64,
        expected_package_urls=("pkg:pypi/unclassified@4.5.6",),
        today=date(2026, 9, 11),
    )

    assert evidence.status == "fail"
    assert evidence.findings[0].criterion_id == "DEP302"
    assert evidence.findings[0].subject == "pkg:pypi/unclassified@4.5.6"


def test_locked_graph_license_evidence_rejects_duplicate_observed_package(tmp_path: Path) -> None:
    """Duplicate distribution metadata makes a candidate inventory ambiguous and fails closed."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    with pytest.raises(ValueError, match="duplicate package URL"):
        checker.build_license_evidence(
            [
                {"name": "example-base", "version": "2.0.0", "license_expression": "MIT"},
                {"name": "Example_Base", "version": "2.0.0", "license_expression": "MIT"},
            ],
            policy,
            scope="locked-all-groups-all-extras",
            revision="d" * 40,
            export_policy_sha256="e" * 64,
            sbom_sha256="f" * 64,
            expected_package_urls=("pkg:pypi/example-base@2.0.0",),
            today=date(2026, 9, 11),
        )


def test_locked_graph_license_evidence_rejects_inventory_outside_locked_requirements(tmp_path: Path) -> None:
    """Platform-specific lock members may be absent, but installed packages must be locked."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    evidence = checker.build_license_evidence(
        [{"name": "example-base", "version": "2.0.0", "license_expression": "MIT"}],
        policy,
        scope="locked-all-groups-all-extras",
        revision="d" * 40,
        export_policy_sha256="e" * 64,
        sbom_sha256="f" * 64,
        expected_package_urls=("pkg:pypi/not-installed@1.0.0",),
        today=date(2026, 9, 11),
    )

    assert evidence.status == "fail"
    assert {finding.criterion_id for finding in evidence.findings} == {"DEP305"}


def test_non_allowlisted_license_fails_even_if_upstream_output_is_clean(tmp_path: Path) -> None:
    """The local allowlist remains authoritative for dependency-review output."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))
    changes = [
        {
            "change_type": "added",
            "manifest": "uv.lock",
            "ecosystem": "pip",
            "name": "copyleft-example",
            "version": "1.0.0",
            "package_url": "pkg:pypi/copyleft-example@1.0.0",
            "license": "GPL-3.0-only",
        }
    ]

    report = checker.review_dependency_changes(changes, policy, today=date(2026, 9, 11))

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP101"


def test_vulnerable_dependency_change_fails_with_advisory_evidence(tmp_path: Path) -> None:
    """The retained supplement identifies the vulnerable package and advisory."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))
    changes = [
        {
            "change_type": "added",
            "manifest": "uv.lock",
            "ecosystem": "pip",
            "name": "vulnerable-example",
            "version": "1.0.0",
            "package_url": "pkg:pypi/vulnerable-example@1.0.0",
            "license": "MIT",
            "vulnerabilities": [
                {
                    "severity": "moderate",
                    "advisory_ghsa_id": "GHSA-xxxx-yyyy-zzzz",
                    "advisory_summary": "Example advisory",
                    "advisory_url": "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz",
                }
            ],
        }
    ]

    report = checker.review_dependency_changes(changes, policy, today=date(2026, 9, 11), upstream_outcome="failure")

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP104"
    assert "GHSA-xxxx-yyyy-zzzz" in report.findings[0].message


def test_unexplained_upstream_failure_fails_closed(tmp_path: Path) -> None:
    """An action failure without a policy finding is treated as control unavailability."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    report = checker.review_dependency_changes([], policy, upstream_outcome="failure")

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP100"


def test_vulnerable_audit_result_preserves_fix_guidance(tmp_path: Path) -> None:
    """A scanner finding is attributable and retains package, advisory, and fixes."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))
    raw = {
        "schema": {"version": "preview"},
        "summary": {"audited_packages": 1, "vulnerabilities": 1, "adverse_statuses": 0},
        "vulnerabilities": [
            {
                "dependency": {"name": "demo", "version": "1.0.0"},
                "id": "GHSA-xxxx-yyyy-zzzz",
                "display_id": "GHSA-xxxx-yyyy-zzzz",
                "aliases": ["CVE-2026-0001"],
                "summary": "Test vulnerability",
                "description": None,
                "link": "https://osv.dev/vulnerability/GHSA-xxxx-yyyy-zzzz",
                "fix_versions": ["1.0.1"],
                "published": "2026-09-01T00:00:00Z",
                "modified": "2026-09-02T00:00:00Z",
            }
        ],
        "adverse_statuses": [],
    }

    evidence = checker.build_audit_evidence(
        raw,
        policy,
        scope="runtime-all-extras",
        revision="a" * 40,
        tool_version="uv 0.12.5",
        scanner_exit_code=1,
    )

    assert evidence.status == "fail"
    assert evidence.findings[0].package == "demo"
    assert evidence.findings[0].fixed_versions == ("1.0.1",)
    assert evidence.findings[0].advisory == "GHSA-xxxx-yyyy-zzzz"


def test_scanner_failure_is_error_not_clean_result(tmp_path: Path) -> None:
    """Unavailable or malformed scanner output cannot become a zero-finding pass."""
    policy = checker.load_policy(_policy(tmp_path), today=date(2026, 9, 11))

    with pytest.raises(ValueError, match="scanner exited with 2"):
        checker.build_audit_evidence(
            {},
            policy,
            scope="development",
            revision="b" * 40,
            tool_version="uv 0.12.5",
            scanner_exit_code=2,
        )


def test_audit_cli_persists_machine_readable_scanner_error(tmp_path: Path) -> None:
    """A transport or scanner failure leaves bounded evidence instead of an empty artifact."""
    policy_path = _policy(tmp_path)
    raw_path = tmp_path / "raw.json"
    raw_path.write_text("", encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"

    exit_code = cli_checker.main(
        [
            "audit-report",
            "--policy",
            str(policy_path),
            "--input",
            str(raw_path),
            "--output",
            str(evidence_path),
            "--scope",
            "development",
            "--revision",
            "c" * 40,
            "--tool-version",
            "uv 0.12.5",
            "--scanner-exit-code",
            "2",
        ]
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert evidence["status"] == "error"
    assert evidence["scanner_exit_code"] == 2
    assert "no valid audit result" in evidence["error"]


def test_license_evidence_cli_persists_resolved_distribution_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate environment itself supplies the license observations to the policy model."""

    class Headers(dict[str, str]):
        @property
        def json(self) -> dict[str, str]:
            return {key.casefold().replace("-", "_"): value for key, value in self.items()}

    class Distribution:
        def __init__(self, location: Path) -> None:
            self.version = "2.0.0"
            self.metadata = Headers({"Name": "example-base", "License-Expression": "MIT"})

            self._location = location

        def locate_file(self, path: str) -> Path:
            assert path == ""
            return self._location

    distribution = Distribution(tmp_path / "site-packages")
    duplicate_path_alias = Distribution(tmp_path / "lib64" / ".." / "site-packages")
    monkeypatch.setattr(cli_checker.metadata, "distributions", lambda: [distribution, duplicate_path_alias])
    evidence_path = tmp_path / "licenses.json"
    sbom_path = tmp_path / "locked-graph.cdx.json"
    sbom_path.write_text(json.dumps({"components": [{"purl": "pkg:pypi/example-base@2.0.0"}]}), encoding="utf-8")
    requirements_path = tmp_path / "locked.requirements.txt"
    requirements_path.write_text("example-base==2.0.0\ncolorama==0.4.6 ; sys_platform == 'win32'\n", encoding="utf-8")

    exit_code = cli_checker.main(
        [
            "license-evidence",
            "--policy",
            str(_policy(tmp_path)),
            "--output",
            str(evidence_path),
            "--scope",
            "locked-all-groups-all-extras",
            "--revision",
            "d" * 40,
            "--export-policy-sha256",
            "e" * 64,
            "--sbom",
            str(sbom_path),
            "--platform-requirements",
            str(requirements_path),
        ]
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert evidence["status"] == "pass"
    assert evidence["packages"] == [{"license_expression": "MIT", "package_url": "pkg:pypi/example-base@2.0.0"}]


def test_license_expression_uses_unambiguous_legacy_metadata() -> None:
    """Legacy License fields and one mapped classifier normalize to SPDX."""

    class Metadata(dict[str, str]):
        @property
        def json(self) -> dict[str, str]:
            return {key.casefold().replace("-", "_"): value for key, value in self.items()}

        def get_all(self, key: str) -> list[str]:
            return ["License :: OSI Approved :: Apache Software License"] if key == "Classifier" else []

    class Distribution:
        metadata = Metadata({"License": "MIT"})

    assert cli_checker._license_expression(Distribution()) == "MIT"
    Distribution.metadata = Metadata()
    assert cli_checker._license_expression(Distribution()) == "Apache-2.0"


def test_license_expression_reads_only_declared_license_files(tmp_path: Path) -> None:
    """PEP 639 files preserve every recognized license notice they declare."""

    class Metadata(dict[str, str]):
        @property
        def json(self) -> dict[str, str]:
            return {key.casefold().replace("-", "_"): value for key, value in self.items()}

        def get_all(self, key: str) -> list[str]:
            return [self[key]] if key == "License-File" and key in self else []

    class Distribution:
        metadata = Metadata({"License-File": "LICENSE"})
        files = ("example_base-2.0.0.dist-info/LICENSE", "example_base-2.0.0.dist-info/NOT_A_LICENSE")

        def locate_file(self, file: str) -> Path:
            return tmp_path / file.rsplit("/", maxsplit=1)[-1]

    (tmp_path / "LICENSE").write_text(
        "Licensed under the Apache License, Version 2.0\n"
        "Redistribution and use in source and binary forms\n"
        "Neither the name of the example project nor its contributors\n",
        encoding="utf-8",
    )
    (tmp_path / "NOT_A_LICENSE").write_text(
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n",
        encoding="utf-8",
    )

    assert cli_checker._license_expression(Distribution()) == "Apache-2.0 AND BSD-3-Clause"


@pytest.mark.parametrize(
    ("license_field", "expected"),
    [
        ("Apache-2.0 AND MIT", "Apache-2.0 AND MIT"),
        ("BSD 3-Clause OR Apache-2.0", "BSD-3-Clause OR Apache-2.0"),
        ("MIT OR Apache-2.0", "MIT OR Apache-2.0"),
        ("MPL-2.0 AND MIT", "MPL-2.0 AND MIT"),
        (
            "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy",
            "MIT",
        ),
    ],
)
def test_license_expression_normalizes_recognized_legacy_expressions(license_field: str, expected: str) -> None:
    """Recognized legacy metadata remains evidence; arbitrary prose does not."""

    class Metadata(dict[str, str]):
        @property
        def json(self) -> dict[str, str]:
            return {key.casefold().replace("-", "_"): value for key, value in self.items()}

    class Distribution:
        metadata = Metadata({"License": license_field})

    assert cli_checker._license_expression(Distribution()) == expected


def test_platform_requirements_rejects_non_exact_or_duplicated_packages(tmp_path: Path) -> None:
    """The environment comparison accepts only uv-style exact pins."""
    requirements_path = tmp_path / "locked.requirements.txt"
    requirements_path.write_text("example-base>=2.0.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one version"):
        cli_checker._locked_requirements_package_urls(requirements_path)


def test_dependency_review_cli_emits_bounded_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CI receives a stable machine-readable failure artifact."""
    policy_path = _policy(tmp_path)
    changes_path = tmp_path / "changes.json"
    changes_path.write_text(
        json.dumps(
            [
                {
                    "change_type": "added",
                    "manifest": "uv.lock",
                    "ecosystem": "pip",
                    "name": "missing-license",
                    "version": "1.0.0",
                    "package_url": "pkg:pypi/missing-license@1.0.0",
                    "license": None,
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = cli_checker.main(
        [
            "dependency-review",
            "--policy",
            str(policy_path),
            "--changes",
            str(changes_path),
            "--today",
            "2026-09-11",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["findings"][0]["criterion_id"] == "DEP102"


@pytest.mark.parametrize(
    ("workflow_name", "required_text"),
    [
        ("dependency-review.yml", "if: github.event.repository.visibility == 'public'"),
        ("dependency-audit.yml", "uv audit --locked --no-dev --output-format json"),
        ("codeql.yml", "if: github.event.repository.visibility == 'public'"),
        ("scorecard.yml", "if: github.event.repository.visibility == 'public'"),
    ],
)
def test_live_supply_chain_workflows_are_staged(workflow_name: str, required_text: str) -> None:
    """Every independent control is source-visible before repository cutover."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")

    assert required_text in workflow


def test_dependabot_has_separate_uv_and_action_update_entries() -> None:
    """Python lock updates and workflow pin updates remain independently reviewable."""
    config = (_REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert config.count("package-ecosystem:") == 2
    assert 'package-ecosystem: "uv"' in config
    assert 'package-ecosystem: "github-actions"' in config
