"""Strict data model and repository checks for fieldkit public releases."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path("docs/release-readiness/release-policy.json")
PYPROJECT_PATH = Path("pyproject.toml")
REPOSITORY_POLICY_PATHS = (POLICY_PATH, PYPROJECT_PATH)

_PUBLIC_CONTRACT = (
    "cli-commands-options",
    "machine-readable-output-schemas",
    "exit-codes",
    "configuration-keys",
    "persisted-data-schemas",
    "explicitly-public-python-imports",
)
_REQUIRED_ASSETS = ("wheel", "sdist", "cyclonedx-json", "sha256sums", "release-evidence")
_RECOVERY_ACTIONS = ("document", "yank-when-harmful-or-unusable", "publish-successor-version")
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True, order=True)
class Finding:
    """One stable release-policy drift finding."""

    criterion_id: str
    subject: str
    message: str


@dataclass(frozen=True)
class Report:
    """Versioned result for checked-in release-policy alignment."""

    schema_version: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """Return whether package metadata and release policy agree."""
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        """Render the stable machine-readable result."""
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True)
class ReleasePolicy:
    """Validated public release commitments used by automation and documentation."""

    distribution: str
    command: str
    import_package: str
    version_source: str
    first_public_version: str
    versioning_scheme: str
    supported_line: str
    support_policy_source: str
    public_contract: tuple[str, ...]
    workflow_path: str
    candidate_trigger: str
    production_trigger: str
    testpypi_environment: str
    pypi_environment: str
    credential: str
    stored_index_tokens: bool
    build_policy: str
    checksum: str
    sbom_scope: str
    required_assets: tuple[str, ...]
    allow_overwrite: bool
    allow_version_reuse: bool
    defective_release_actions: tuple[str, ...]


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON/TOML object with string keys."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _exact_keys(value: dict[str, object], expected: set[str], subject: str) -> None:
    """Reject missing or unknown schema fields."""
    if set(value) != expected:
        raise ValueError(f"{subject} keys must be exactly {sorted(expected)}")


def _string(value: object, subject: str) -> str:
    """Require one non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def _strings(value: object, subject: str) -> tuple[str, ...]:
    """Require a non-empty list of unique non-empty strings."""
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{subject} must be a non-empty string list")
    result = tuple(item for item in value if isinstance(item, str))
    if len(result) != len(set(result)):
        raise ValueError(f"{subject} must not contain duplicates")
    return result


def _boolean(value: object, subject: str) -> bool:
    """Require an actual JSON boolean rather than a truthy substitute."""
    if not isinstance(value, bool):
        raise ValueError(f"{subject} must be a boolean")
    return value


def _require_exact(value: object, expected: object, subject: str) -> None:
    """Reject a policy value that weakens or renames a ratified invariant."""
    if value != expected:
        raise ValueError(f"{subject} must be {expected!r}")


def load_policy(path: Path = REPO_ROOT / POLICY_PATH) -> ReleasePolicy:
    """Load and strictly validate the checked-in public release policy."""
    raw = _object(json.loads(path.read_text(encoding="utf-8")), str(POLICY_PATH))
    _exact_keys(
        raw, {"schema_version", "package", "compatibility", "workflow", "artifacts", "recovery"}, str(POLICY_PATH)
    )
    _require_exact(raw.get("schema_version"), 1, "schema_version")

    package = _object(raw.get("package"), "package")
    _exact_keys(
        package, {"distribution", "command", "import_package", "version_source", "first_public_version"}, "package"
    )
    compatibility = _object(raw.get("compatibility"), "compatibility")
    _exact_keys(
        compatibility, {"scheme", "supported_line", "support_policy_source", "public_contract"}, "compatibility"
    )
    workflow = _object(raw.get("workflow"), "workflow")
    _exact_keys(
        workflow,
        {
            "path",
            "candidate_trigger",
            "production_trigger",
            "testpypi_environment",
            "pypi_environment",
            "credential",
            "stored_index_tokens",
        },
        "workflow",
    )
    artifacts = _object(raw.get("artifacts"), "artifacts")
    _exact_keys(artifacts, {"build_policy", "checksum", "sbom_scope", "required"}, "artifacts")
    recovery = _object(raw.get("recovery"), "recovery")
    _exact_keys(recovery, {"allow_overwrite", "allow_version_reuse", "defective_release_actions"}, "recovery")

    first_public_version = _string(package.get("first_public_version"), "package.first_public_version")
    if _SEMVER.fullmatch(first_public_version) is None:
        raise ValueError("package.first_public_version must be a stable SemVer version")
    public_contract = _strings(compatibility.get("public_contract"), "compatibility.public_contract")
    required_assets = _strings(artifacts.get("required"), "artifacts.required")
    recovery_actions = _strings(recovery.get("defective_release_actions"), "recovery.defective_release_actions")

    _require_exact(package.get("distribution"), "fieldkit-cli", "package.distribution")
    _require_exact(package.get("command"), "fieldkit", "package.command")
    _require_exact(package.get("import_package"), "fieldkit", "package.import_package")
    _require_exact(package.get("version_source"), "pyproject.toml", "package.version_source")
    _require_exact(compatibility.get("scheme"), "semantic-versioning-2.0.0", "compatibility.scheme")
    _require_exact(compatibility.get("supported_line"), "latest-patch-of-latest-minor", "compatibility.supported_line")
    _require_exact(compatibility.get("support_policy_source"), "SUPPORT.md", "compatibility.support_policy_source")
    _require_exact(public_contract, _PUBLIC_CONTRACT, "compatibility.public_contract")
    _require_exact(workflow.get("path"), ".github/workflows/release.yml", "workflow.path")
    _require_exact(workflow.get("candidate_trigger"), "protected-main-workflow-dispatch", "workflow.candidate_trigger")
    _require_exact(workflow.get("production_trigger"), "protected-signed-semver-tag", "workflow.production_trigger")
    _require_exact(workflow.get("testpypi_environment"), "testpypi", "workflow.testpypi_environment")
    _require_exact(workflow.get("pypi_environment"), "pypi", "workflow.pypi_environment")
    _require_exact(workflow.get("credential"), "oidc-trusted-publishing", "workflow.credential")
    _require_exact(
        _boolean(workflow.get("stored_index_tokens"), "workflow.stored_index_tokens"),
        False,
        "workflow.stored_index_tokens",
    )
    _require_exact(artifacts.get("build_policy"), "build-once-promote-by-digest", "artifacts.build_policy")
    _require_exact(artifacts.get("checksum"), "sha256", "artifacts.checksum")
    _require_exact(artifacts.get("sbom_scope"), "installed-all-runtime-profile", "artifacts.sbom_scope")
    _require_exact(required_assets, _REQUIRED_ASSETS, "artifacts.required")
    _require_exact(
        _boolean(recovery.get("allow_overwrite"), "recovery.allow_overwrite"), False, "recovery.allow_overwrite"
    )
    _require_exact(
        _boolean(recovery.get("allow_version_reuse"), "recovery.allow_version_reuse"),
        False,
        "recovery.allow_version_reuse",
    )
    _require_exact(recovery_actions, _RECOVERY_ACTIONS, "recovery")

    return ReleasePolicy(
        distribution=_string(package.get("distribution"), "package.distribution"),
        command=_string(package.get("command"), "package.command"),
        import_package=_string(package.get("import_package"), "package.import_package"),
        version_source=_string(package.get("version_source"), "package.version_source"),
        first_public_version=first_public_version,
        versioning_scheme=_string(compatibility.get("scheme"), "compatibility.scheme"),
        supported_line=_string(compatibility.get("supported_line"), "compatibility.supported_line"),
        support_policy_source=_string(
            compatibility.get("support_policy_source"), "compatibility.support_policy_source"
        ),
        public_contract=public_contract,
        workflow_path=_string(workflow.get("path"), "workflow.path"),
        candidate_trigger=_string(workflow.get("candidate_trigger"), "workflow.candidate_trigger"),
        production_trigger=_string(workflow.get("production_trigger"), "workflow.production_trigger"),
        testpypi_environment=_string(workflow.get("testpypi_environment"), "workflow.testpypi_environment"),
        pypi_environment=_string(workflow.get("pypi_environment"), "workflow.pypi_environment"),
        credential=_string(workflow.get("credential"), "workflow.credential"),
        stored_index_tokens=False,
        build_policy=_string(artifacts.get("build_policy"), "artifacts.build_policy"),
        checksum=_string(artifacts.get("checksum"), "artifacts.checksum"),
        sbom_scope=_string(artifacts.get("sbom_scope"), "artifacts.sbom_scope"),
        required_assets=required_assets,
        allow_overwrite=False,
        allow_version_reuse=False,
        defective_release_actions=recovery_actions,
    )


def validate_repository(repo_root: Path = REPO_ROOT) -> Report:
    """Validate release identity against authoritative package metadata."""
    policy = load_policy(repo_root / POLICY_PATH)
    pyproject = _object(tomllib.loads((repo_root / PYPROJECT_PATH).read_text(encoding="utf-8")), str(PYPROJECT_PATH))
    project = _object(pyproject.get("project"), "project")
    scripts = _object(project.get("scripts"), "project.scripts")
    findings: list[Finding] = []
    if project.get("name") != policy.distribution:
        findings.append(Finding("REL100", str(PYPROJECT_PATH), f"project distribution must be {policy.distribution}"))
    if project.get("version") != policy.first_public_version:
        findings.append(
            Finding("REL101", str(PYPROJECT_PATH), f"project version must be {policy.first_public_version}")
        )
    if scripts.get(policy.command) != "fieldkit.__main__:main":
        findings.append(
            Finding("REL102", str(PYPROJECT_PATH), "fieldkit console entry point must remain fieldkit.__main__:main")
        )
    if project.get("license") != "Apache-2.0":
        findings.append(Finding("REL103", str(PYPROJECT_PATH), "project license must remain Apache-2.0"))
    return Report(1, tuple(sorted(findings)))
