"""Contracts for compatibility claims and CI evidence alignment."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_compatibility_policy.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_compatibility_policy", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.fixture
def compatibility_repo(tmp_path: Path) -> Path:
    for relative in (
        checker.POLICY_PATH,
        checker.WORKFLOW_PATH,
        checker.DOCUMENTATION_PATH,
        checker.COMPATIBILITY_DOCUMENTATION_PATH,
        checker.PYPROJECT_PATH,
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    return tmp_path


def test_live_compatibility_contract_passes() -> None:
    """Checked-in support claims agree with metadata, documentation, and CI."""
    report = checker.validate(_REPO_ROOT)

    assert report.ok
    assert report.findings == ()


def test_documentation_block_renders_every_supported_system() -> None:
    block = checker._documentation_block(
        ("3.11",),
        ("ubuntu-24.04", "macos-15", "example-os"),
        "fedora:43",
        "system",
        "3.11",
        ("ubuntu-24.04", "macos-15"),
    )

    assert "`ubuntu-24.04`, `macos-15`, and `example-os`" in block


def test_compatibility_table_renders_from_the_support_policy() -> None:
    table = checker._compatibility_table(
        ("3.11", "3.12"),
        ("ubuntu-24.04", "macos-15"),
        "fedora:43",
    )

    assert "| Ubuntu | 3.11, 3.12 | Supported core |" in table
    assert "| Fedora/RHEL family | Fedora 43 system Python | Supported core |" in table


def test_compatibility_table_cannot_drift_from_policy(compatibility_repo: Path) -> None:
    """The public compatibility matrix is rendered from the checked policy."""
    documentation = compatibility_repo / checker.COMPATIBILITY_DOCUMENTATION_PATH
    documentation.write_text(
        documentation.read_text(encoding="utf-8").replace("Supported core", "Experimental", 1), encoding="utf-8"
    )

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP107" for finding in report.findings)


def test_supported_python_missing_from_workflow_fails(compatibility_repo: Path) -> None:
    """Every advertised Python version must appear in the hosted matrix."""
    workflow = compatibility_repo / checker.WORKFLOW_PATH
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace('second: "3.14"', 'second: "3.13"'), encoding="utf-8"
    )

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP101" for finding in report.findings)


def test_policy_rejects_unknown_schema_key(compatibility_repo: Path) -> None:
    """Unknown compatibility claims cannot enter the policy without validation."""
    policy_path = compatibility_repo / checker.POLICY_PATH
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["unreviewed"] = True
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="keys must be exactly"):
        checker.validate(compatibility_repo)


def test_documented_support_cannot_drift_from_policy(compatibility_repo: Path) -> None:
    """User-facing platform promises remain generated from the support policy."""
    docs = compatibility_repo / checker.DOCUMENTATION_PATH
    docs.write_text(
        docs.read_text(encoding="utf-8").replace("Native Windows is experimental", "Windows is supported"),
        encoding="utf-8",
    )

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP105" for finding in report.findings)


def test_workflow_rejects_secret_or_pull_request_target_use(compatibility_repo: Path) -> None:
    """Fork-safe compatibility checks receive neither secrets nor elevated triggers."""
    workflow = compatibility_repo / checker.WORKFLOW_PATH
    workflow.write_text(
        workflow.read_text(encoding="utf-8") + "\n# pull_request_target secrets.TOKEN\n", encoding="utf-8"
    )

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP102" for finding in report.findings)


def test_workflow_requires_all_profile_artifact_smoke_for_each_supported_os(compatibility_repo: Path) -> None:
    """Each supported OS installs the complete optional profile from the wheel."""
    workflow = compatibility_repo / checker.WORKFLOW_PATH
    workflow.write_text(workflow.read_text(encoding="utf-8").replace("--profile all", "", 1), encoding="utf-8")

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP106" for finding in report.findings)


def test_optional_profile_smoke_must_remain_bound_to_macos_job(compatibility_repo: Path) -> None:
    workflow = compatibility_repo / checker.WORKFLOW_PATH
    text = workflow.read_text(encoding="utf-8")
    macos_start = text.index("  optional-macos:")
    fedora_start = text.index("  fedora-family:")
    macos = text[macos_start:fedora_start].replace("--profile all", "")
    text = text[:macos_start] + macos + text[fedora_start:] + "\n# --profile all\n"
    workflow.write_text(text, encoding="utf-8")

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP106" and "macos-15" in finding.subject for finding in report.findings)


def test_optional_profile_python_setup_must_precede_smoke(compatibility_repo: Path) -> None:
    workflow = compatibility_repo / checker.WORKFLOW_PATH
    text = workflow.read_text(encoding="utf-8")
    start = text.index("  optional-macos:")
    end = text.index("  fedora-family:")
    job = text[start:end]
    setup_start = job.index("      - uses: actions/setup-python@")
    setup_end = job.index("      - name:", setup_start)
    setup = job[setup_start:setup_end]
    job = job[:setup_start] + job[setup_end:] + setup
    workflow.write_text(text[:start] + job + text[end:], encoding="utf-8")

    report = checker.validate(compatibility_repo)

    assert any(finding.criterion_id == "COMP106" and "macos-15" in finding.subject for finding in report.findings)


def test_json_cli_failure_is_machine_readable(compatibility_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs = compatibility_repo / checker.DOCUMENTATION_PATH
    docs.write_text("no support statement\n", encoding="utf-8")

    exit_code = checker.main(["--repo-root", str(compatibility_repo), "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "fail"
    assert payload["findings"][0]["criterion_id"] == "COMP105"
