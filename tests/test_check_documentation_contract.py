"""Tests for the versioned public-documentation contract."""

import json
from pathlib import Path

import pytest

from scripts import check_documentation_contract

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_contract(repo: Path, *, fingerprint: str = "sha256:" + "0" * 64, excluded: list[str] | None = None) -> None:
    _write(repo / "Makefile", "artifact-check:\npr-check:\nquality:\n")
    _write(repo / "scripts/generate_cli_docs.py", "")
    _write(repo / "scripts/generate_dep_map.py", "")
    _write(repo / "scripts/check_documentation_contract.py", "")
    _write(repo / "scripts/check_documentation_examples.py", "")
    _write(repo / "scripts/check_roadmap_contract.py", "")
    _write(repo / "scripts/check_documentation_example_scenarios.py", "")
    _write(repo / "scripts/release_workflow_policy.py", "")
    _write(repo / "scripts/check_compatibility_policy.py", "")
    _write(repo / "scripts/smoke_artifact.py", "")
    _write(repo / "tests/test_cli_exit.py", "")
    _write(repo / "tests/test_documentation_configuration_examples.py", "")
    schema = Path(__file__).parents[1] / "docs/release-readiness/documentation-contract.schema.json"
    _write(repo / "docs/release-readiness/documentation-contract.schema.json", schema.read_text(encoding="utf-8"))
    evidence_schema = Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
    _write(repo / "docs/release-readiness/rehearsal-evidence.schema.json", evidence_schema.read_text(encoding="utf-8"))
    contract = {
        "schema_version": 2,
        "roadmap": "ROADMAP.md",
        "example_verifications": {
            "automated.roadmap-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_roadmap_contract.py",
                "mode": "automated",
            },
            "automated.compatibility-policy": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_compatibility_policy.py",
                "mode": "automated",
            },
            "automated.generated-reference": {
                "classification": "generated_reference",
                "evidence": "canonical documentation generator",
                "mode": "automated",
            },
            "automated.generated-dependency-map": {
                "classification": "generated_reference",
                "evidence": "dependency-map documentation generator",
                "mode": "automated",
            },
            "automated.installed-base-artifact": {
                "classification": "safe_automated_command",
                "evidence": "fixed installed-artifact documentation scenarios",
                "mode": "automated",
            },
            "automated.release-workflow-policy": {
                "classification": "safe_automated_command",
                "evidence": "make release-workflow-policy-check",
                "mode": "automated",
            },
            "automated.exit-code-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_cli_exit.py -q",
                "mode": "automated",
            },
            "automated.configuration-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_configuration_examples.py -q",
                "mode": "automated",
            },
            "automated.documentation-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_documentation_contract.py",
                "mode": "automated",
            },
            "manual.credentialed-integration": {
                "classification": "credentialed_manual_integration",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
            "manual.release-cutover": {
                "classification": "exact_release_cutover_proof",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
        },
        "verification": {
            "artifact_smoke": {"evidence": "make artifact-check", "paths": ["README.md"]},
            "contributor_gate": {
                "evidence": "QUALITY_BASE=upstream/main make pr-check",
                "paths": [],
            },
            "generated_reference": {
                "evidence": "uv run python scripts/generate_cli_docs.py --check",
                "paths": [],
            },
            "generated_dependency_map": {
                "evidence": "uv run python scripts/generate_dep_map.py --check",
                "paths": [],
            },
            "live_cutover": {
                "evidence": "ROADMAP.md",
                "paths": ["ROADMAP.md"],
            },
            "policy_validator": {"evidence": "make quality", "paths": []},
            "source_contract": {
                "evidence": "uv run python scripts/check_documentation_contract.py",
                "paths": [],
            },
        },
        "documents": {
            "README.md": {
                "content_type": "overview",
                "reader_action": "Choose an installation path.",
                "sources": ["src/fieldkit/__main__.py"],
                "source_fingerprint": fingerprint,
            },
            "ROADMAP.md": {
                "content_type": "roadmap",
                "reader_action": "Review work that is not shipped.",
                "sources": ["pyproject.toml"],
                "source_fingerprint": fingerprint,
            },
        },
    }
    for path, entry in contract["documents"].items():
        document = repo / path
        if not document.is_file():
            continue
        try:
            inventory = check_documentation_contract._block_inventory(document)
        except ValueError:
            inventory = []
        entry["fenced_blocks"] = [
            {
                "id": f"{path.lower().replace('/', '.').replace('.md', '')}.block-{index}",
                **block,
                "classification": "generated_reference",
                "verification_id": "automated.generated-reference",
            }
            for index, block in enumerate(inventory, start=1)
        ]
    _write(repo / "docs/documentation-contract.json", json.dumps(contract))
    policy = {
        "schema_version": 1,
        "categories": {
            "public_entrypoint": ["README.md", "ROADMAP.md"],
            "public_site": [],
            "public_repository_only": [],
            "private_history_excluded": excluded or [],
        },
        "issue_forms": {
            "required": ["bug.yml"],
            "allowed_labels": ["bug"],
            "contact_links": ["https://example.com/security", "https://example.com/support"],
        },
    }
    _write(repo / "docs/release-readiness/public-surface-policy.json", json.dumps(policy))


def test_validate_accepts_complete_current_contract(tmp_path: Path) -> None:
    """A complete contract with current sources and examples is valid."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n\nPlanned work belongs here.\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert findings == ()


def test_validate_rejects_missing_public_document_contract(tmp_path: Path) -> None:
    """Every Markdown document in the public surface needs a contract record."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    del contract["documents"]["README.md"]
    contract["verification"]["artifact_smoke"]["paths"].remove("README.md")
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC401", "README.md") in findings


def test_validate_rejects_stale_source_fingerprint(tmp_path: Path) -> None:
    """Implementation changes invalidate the owning document fingerprint."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    raise RuntimeError\n")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC406", "README.md") in findings


@pytest.mark.parametrize(
    "phrase",
    [
        "planned first release",
        "not yet supported",
        "future contributors",
        "future work",
        "pending implementation",
        "coming soon",
        "under development",
        "we will support",
        "in flight",
    ],
)
def test_validate_rejects_future_status_outside_roadmap(tmp_path: Path, phrase: str) -> None:
    """Future-status language is reserved for the public roadmap."""
    _write(tmp_path / "README.md", f"# Current behavior\n\nThis is {phrase}.\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC407", "README.md") in findings


def test_validate_rejects_unresolved_source_pattern(tmp_path: Path) -> None:
    """Source patterns must resolve so missing inputs cannot pass silently."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["sources"] = ["src/missing/**"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC405", "README.md") in findings


def test_validate_rejects_changed_fenced_example(tmp_path: Path) -> None:
    """Edited examples require an explicit reviewed inventory refresh."""
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit doctor\n```\n")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC408", "README.md") in findings


def test_validate_rejects_unowned_markdown_table(tmp_path: Path) -> None:
    """A public table cannot bypass the reviewed structured-claim inventory."""
    _write(tmp_path / "README.md", "# Current behavior\n\n| Name | Value |\n| --- | --- |\n| core | local |\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC410", "README.md") in findings


@pytest.mark.parametrize("fence", ["~~~console\nfieldkit --help\n~~~", " ````console\nfieldkit --help\n ````"])
def test_validate_inventories_commonmark_fence_forms(tmp_path: Path, fence: str) -> None:
    """The block inventory recognizes supported CommonMark fence forms."""
    _write(tmp_path / "README.md", f"# Current behavior\n\n{fence}\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    assert check_documentation_contract.validate(tmp_path) == ()


def test_validate_rejects_unclosed_fenced_block(tmp_path: Path) -> None:
    """Malformed unclosed examples fail contract validation."""
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)

    with pytest.raises(ValueError, match="unclosed fenced block"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_source_pattern_outside_repository(tmp_path: Path) -> None:
    """Contract source patterns cannot escape the repository."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["sources"] = ["../private.txt"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_unknown_document_field(tmp_path: Path) -> None:
    """Unknown document metadata fails the closed contract schema."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["unvalidated"] = True
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="Additional properties"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_document_without_verification_owner(tmp_path: Path) -> None:
    """Each public document has exactly one declared verification owner."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["verification"]["artifact_smoke"]["paths"] = []
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one verification owner"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_duplicate_json_key(tmp_path: Path) -> None:
    """Duplicate JSON keys cannot ambiguously override contract values."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    text = contract_path.read_text(encoding="utf-8").replace(
        '"schema_version": 2,', '"schema_version": 2, "schema_version": 2,', 1
    )
    contract_path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_source_removed_from_public_tree(tmp_path: Path) -> None:
    """Public documents cannot depend on sources excluded from the export."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path, excluded=["src/fieldkit/**"])
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC409", "README.md") in findings


def test_validate_rejects_schema_version_one(tmp_path: Path) -> None:
    """The ownership-aware contract cannot silently fall back to hash-only schema v1."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["schema_version"] = 1
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_duplicate_fenced_block_identifier(tmp_path: Path) -> None:
    """Stable example identifiers are unique across the complete public corpus."""
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n\n```text\nLater\n```\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    duplicate = contract["documents"]["README.md"]["fenced_blocks"][0]["id"]
    contract["documents"]["ROADMAP.md"]["fenced_blocks"][0]["id"] = duplicate
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="identifiers must be unique"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_incompatible_example_verification(tmp_path: Path) -> None:
    """A manual classification cannot point at an automated scenario."""
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["fenced_blocks"][0]["classification"] = "credentialed_manual_integration"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="incompatible verification ownership"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_arbitrary_example_verification_route(tmp_path: Path) -> None:
    """Contract authors cannot declare an unenforced route and call it verification."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["example_verifications"]["automated.generated-reference"]["evidence"] = "trust me"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported example verification route"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_missing_example_verification_evidence(tmp_path: Path) -> None:
    """Registered evidence must exist instead of remaining a future filename."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    (tmp_path / "docs/release-readiness/rehearsal-evidence.schema.json").unlink()

    with pytest.raises(ValueError, match="example verification evidence is unavailable"):
        check_documentation_contract.validate(tmp_path)


def test_refresh_blocks_rejects_missing_ownership(tmp_path: Path) -> None:
    """Refreshing hashes cannot manufacture ownership for a newly added example."""
    _write(tmp_path / "README.md", "# Current behavior\n")
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    _write(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")

    with pytest.raises(ValueError, match="ownership must be reviewed"):
        check_documentation_contract.refresh_block_inventory(tmp_path)


def test_refresh_blocks_rejects_reordered_ownership(tmp_path: Path) -> None:
    """Reordering same-count blocks cannot transfer safe ownership to different content."""
    _write(
        tmp_path / "README.md",
        "# Current behavior\n\n```console\nfieldkit --help\n```\n\n```console\nfieldkit doctor\n```\n",
    )
    _write(tmp_path / "ROADMAP.md", "# Roadmap\n")
    _write(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    _write_contract(tmp_path)
    _write(
        tmp_path / "README.md",
        "# Current behavior\n\n```console\nfieldkit doctor\n```\n\n```console\nfieldkit --help\n```\n",
    )

    with pytest.raises(ValueError, match="explicit ownership review"):
        check_documentation_contract.refresh_block_inventory(tmp_path)


def test_main_writes_contract_errors_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Contract-load failures use stderr and the stable error status."""
    result = check_documentation_contract.main(["--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "Documentation contract: ERROR" in captured.err
