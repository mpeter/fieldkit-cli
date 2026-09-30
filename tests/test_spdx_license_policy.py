"""Strict maintained SPDX parsing contracts for both acceptance controls."""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from scripts import _supply_chain_policy as checker

pytestmark = pytest.mark.unit

_MARKER_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.8",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.0.0",
    "platform_system": "Linux",
    "platform_version": "Example kernel",
    "python_full_version": "3.11.8",
    "python_version": "3.11",
    "sys_platform": "linux",
}


def _policy(_path: Path) -> checker.DependencyPolicy:
    return checker.DependencyPolicy(
        1, "low", ("Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "MIT"), "fail-unless-excepted", ()
    )


@pytest.mark.parametrize("mode", ["review", "candidate"])
@pytest.mark.parametrize(
    ("expression", "accepted"),
    [
        ("MIT", True),
        ("ISC", False),
        ("MIT AND Apache-2.0", True),
        ("MIT OR Apache-2.0", True),
        ("(MIT OR BSD-2-Clause) AND Apache-2.0", True),
        ("MIT AND", False),
        ("AND MIT", False),
        ("MIT OR", False),
        ("MIT AND OR Apache-2.0", False),
        ("MIT Apache-2.0", False),
        ("(MIT OR Apache-2.0", False),
        ("MIT OR Apache-2.0)", False),
        ("MIT ()", False),
        ("MIT;", False),
        ("MIT / Apache-2.0", False),
        ("MIT WITH", False),
        ("MIT WITH Apache-2.0", False),
        ("Classpath-exception-2.0 WITH MIT", False),
        ("MIT WITH Unknown-exception", False),
        ("LicenseRef-MIT", False),
        ("DocumentRef-example:LicenseRef-MIT", False),
        ("Unknown-license", False),
        ("MIT OR GPL-3.0-only", False),
        ("MIT AND GPL-3.0-only", False),
        ("GPL-2.0-only WITH Classpath-exception-2.0", False),
        ("MIT WITH LLVM-exception", False),
    ],
)
def test_spdx_expression_policy(tmp_path: Path, mode: str, expression: str, accepted: bool) -> None:
    """Both controls validate complete grammar and screen every license and exception."""
    policy = _policy(tmp_path)
    if mode == "review":
        report = checker.review_dependency_changes(
            [{"change_type": "added", "package_url": "pkg:pypi/example-base@2.0.0", "license": expression}],
            policy,
            today=date(2026, 9, 11),
        )
        assert report.ok is accepted
        findings = report.findings
        criterion = "DEP101"
    else:
        evidence = checker.build_license_evidence(
            [{"name": "example-base", "version": "2.0.0", "license_expression": expression}],
            policy,
            scope="runtime-all-extras",
            revision="d" * 40,
            export_policy_sha256="e" * 64,
            sbom_sha256="f" * 64,
            observations_sha256="a" * 64,
            platform_requirements_sha256="b" * 64,
            marker_environment=_MARKER_ENVIRONMENT,
            expected_package_urls=("pkg:pypi/example-base@2.0.0",),
            today=date(2026, 9, 11),
        )
        assert evidence.status == ("pass" if accepted else "fail")
        findings = evidence.findings
        criterion = "DEP301"
    assert {finding.criterion_id for finding in findings} == (set() if accepted else {criterion})


@pytest.mark.parametrize("mode", ["review", "candidate"])
@pytest.mark.parametrize("failure", ["missing", "parse-error", "invalid-api"])
def test_spdx_validator_unavailability_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    def unavailable(_name: str) -> ModuleType:
        raise ImportError("parser absent")

    class InvalidParser:
        def parse(self, _expression: str, *, validate: bool, strict: bool) -> object:
            assert validate and strict
            raise RuntimeError("parser failed")

        def license_keys(self, _parsed: object) -> list[str]:
            return ["MIT"]

    module = ModuleType("license_expression")
    monkeypatch.setattr(
        module, "get_spdx_licensing", InvalidParser if failure == "parse-error" else None, raising=False
    )
    monkeypatch.setattr(checker, "import_module", unavailable if failure == "missing" else lambda _name: module)

    test_spdx_expression_policy(tmp_path, mode, "MIT", False)


def test_policy_only_validation_does_not_require_spdx_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_name: str) -> ModuleType:
        raise ImportError("parser absent")

    monkeypatch.setattr(checker, "import_module", unavailable)

    report = checker.validate_repository(Path(__file__).resolve().parent.parent, today=date(2026, 9, 11))

    assert report.ok


def test_with_parsing_retains_the_exception_without_asserting_legal_applicability() -> None:
    identifiers = checker._license_identifiers("MIT WITH LLVM-exception")

    assert identifiers == ("MIT", "LLVM-exception")


def test_with_parsing_rejects_reversed_operand_categories() -> None:
    with pytest.raises(ValueError, match="SPDX expression is invalid"):
        checker._license_identifiers("Classpath-exception-2.0 WITH MIT")


@pytest.mark.parametrize("mode", ["review", "candidate"])
@pytest.mark.parametrize("expression", ["MIT", "MIT AND"])
@pytest.mark.parametrize("installed_parser", [False, True])
def test_isolated_checker_uses_installed_parser_before_candidate_root(
    tmp_path: Path, mode: str, expression: str, installed_parser: bool
) -> None:
    """A proposed root module must neither replace QA tools nor execute on import."""
    source_scripts = Path(__file__).resolve().parent.parent / "scripts"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_supply_chain_policy.py", "_supply_chain_policy.py", "runtime_license_inventory.py"):
        shutil.copy2(source_scripts / name, scripts / name)
    shadow = tmp_path / "license_expression.py"
    shadow.write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_suffix('.imported').write_text('shadow executed', encoding='utf-8')\n"
        "class Parser:\n"
        "    def parse(self, expression, **kwargs): return 'MIT'\n"
        "    def license_keys(self, parsed): return ['MIT']\n"
        "def get_spdx_licensing(): return Parser()\n",
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vulnerability_threshold": "low",
                "allowed_spdx_licenses": ["MIT"],
                "unknown_license_policy": "fail-unless-excepted",
                "license_exceptions": [],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    arguments = ["--policy", str(policy), "--output", str(output)]
    if mode == "review":
        changes = tmp_path / "changes.json"
        changes.write_text(
            json.dumps(
                [
                    {
                        "change_type": "added",
                        "package_url": "pkg:pypi/example-base@2.0.0",
                        "license": expression,
                    }
                ]
            ),
            encoding="utf-8",
        )
        command = "dependency-review"
        arguments.extend(["--changes", str(changes)])
        criterion = "DEP101"
    else:
        observations = tmp_path / "observations.json"
        contents = json.dumps(
            {
                "schema_version": 1,
                "packages": [{"name": "example-base", "version": "2.0.0", "license_expression": expression}],
                "marker_environment": _MARKER_ENVIRONMENT,
            }
        ).encode("utf-8")
        observations.write_bytes(contents)
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("example-base==2.0.0\n", encoding="utf-8")
        sbom = tmp_path / "sbom.json"
        sbom.write_text(json.dumps({"components": [{"purl": "pkg:pypi/example-base@2.0.0"}]}), encoding="utf-8")
        command = "license-evidence"
        arguments.extend(
            [
                "--observations",
                str(observations),
                "--observations-sha256",
                hashlib.sha256(contents).hexdigest(),
                "--platform-requirements",
                str(requirements),
                "--sbom",
                str(sbom),
                "--scope",
                "runtime-all-extras",
                "--revision",
                "d" * 40,
                "--export-policy-sha256",
                "e" * 64,
            ]
        )
        criterion = "DEP301"

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            *([] if installed_parser else ["-S"]),
            str(scripts / "check_supply_chain_policy.py"),
            command,
            *arguments,
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )

    expected = (0 if expression == "MIT" else 1) if installed_parser else (1 if mode == "review" else 2)
    assert result.returncode == expected, result.stderr
    assert not shadow.with_suffix(".imported").exists()
    report = json.loads(output.read_text(encoding="utf-8"))
    if installed_parser:
        assert {finding["criterion_id"] for finding in report["findings"]} == (
            set() if expression == "MIT" else {criterion}
        )
    elif mode == "review":
        assert report["findings"][0]["criterion_id"] == criterion
    else:
        assert report["status"] == "error"
