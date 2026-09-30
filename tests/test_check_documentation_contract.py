"""Tests for the versioned public-documentation contract."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.cli_registry import CommandNode, walk_cli
from scripts import check_documentation_contract, documentation_manual
from tests.documentation_contract_support import write_contract_validation_fixture, write_fixture_file

pytestmark = pytest.mark.unit

_PRODUCTION_MANUAL_SCENARIOS = documentation_manual.MANUAL_SCENARIOS


@pytest.fixture(autouse=True)
def fixture_manual_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The small fixture contract has no registered manual examples."""
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", ())


@pytest.mark.parametrize("mutation", ["unknown", "deleted", "reclassified", "digest", "document"])
def test_manual_catalog_cannot_replace_controller_registration(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    repo = Path(__file__).parents[1]
    contract = check_documentation_contract._load_json(repo / "docs/documentation-contract.json")
    documents = check_documentation_contract._documents(contract)
    blocks = documents["docs/guides/gmail.md"]["fenced_blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    if mutation == "unknown":
        block["id"] = "candidate.invented-manual-proof"
    elif mutation == "deleted":
        blocks.pop(0)
    elif mutation == "reclassified":
        block["classification"] = "structural_assertion"
        block["verification_id"] = "automated.documentation-contract"
    elif mutation == "digest":
        block["sha256"] = "0" * 64
    else:
        readme_blocks = documents["README.md"]["fenced_blocks"]
        assert isinstance(readme_blocks, list)
        readme_blocks.append(blocks.pop(0))

    with pytest.raises(ValueError, match="manual scenario registry"):
        check_documentation_contract._validate_example_ownership(contract, documents)


def test_prepared_manual_registry_matches_current_catalog_without_approving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    repo = Path(__file__).parents[1]
    contract = check_documentation_contract._load_json(repo / "docs/documentation-contract.json")

    result = documentation_manual.manual_scenarios(contract["documents"])

    assert result == _PRODUCTION_MANUAL_SCENARIOS
    assert len(result) == 47
    assert all(scenario.behavioral_verifier is None for scenario in result)
    gmail_sync = [
        scenario
        for scenario in result
        if scenario.document == "docs/guides/gmail.md"
        and scenario.sha256 == "13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c"
    ]
    assert len(gmail_sync) == 5
    assert len({scenario.scenario_id for scenario in gmail_sync}) == 5
    assert len({scenario.precondition_id for scenario in gmail_sync}) == 5


@pytest.mark.parametrize(
    "owner,phase",
    [
        ("automated.contributor-journey", "leaf"),
        ("automated.contributor-instruction-structure", "contributor_journey"),
        ("automated.contributor-journey", "unknown"),
    ],
)
def test_example_phase_cannot_reassign_canonical_owner(owner: str, phase: str) -> None:
    repo = Path(__file__).parents[1]
    contract = check_documentation_contract._load_json(repo / "docs/documentation-contract.json")
    verifications = contract["example_verifications"]
    assert isinstance(verifications, dict)
    verifications[owner]["phase"] = phase
    with pytest.raises(ValueError, match="unsupported example verification route"):
        check_documentation_contract._validate_example_ownership(
            contract, check_documentation_contract._documents(contract)
        )


def test_real_contract_assigns_one_owner_per_registered_document() -> None:
    repo = Path(__file__).parents[1]
    contract = check_documentation_contract._load_json(repo / "docs/documentation-contract.json")
    assert check_documentation_contract._validate_schema(repo, contract) is None
    documents = check_documentation_contract._documents(contract)

    assert check_documentation_contract._validate_verification_ownership(contract, documents) is None


@pytest.mark.parametrize("folded", [False, True])
def test_skill_index_verbose_command_displays_full_frontmatter_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folded: bool
) -> None:
    from fieldkit.commands.skill import _runner
    from fieldkit.commands.skill.cli import cli

    first = "Inspect the selected fictional account."
    second = "Retain this second sentence beyond the normal abbreviated list description."
    description = f">\n  {first}\n  {second}" if folded else f'"{first} {second}"'
    skill = tmp_path / "sample" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(f"---\nname: sample\ndescription: {description}\n---\n", encoding="utf-8")
    monkeypatch.setattr(_runner, "_skills_dir", lambda: tmp_path)

    _runner._load_all_skills.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["list", "--verbose"])
    finally:
        _runner._load_all_skills.cache_clear()

    assert result.exit_code == 0
    assert f"{first} {second}" in result.output
    assert "sample" in result.output


def test_skill_index_live_page_matches_packaged_entrypoints() -> None:
    """The semantic owner consumes and validates the shipped skill index itself."""
    repo = Path(__file__).parents[1]

    findings = check_documentation_contract._skill_index_findings(repo)

    assert findings == ()


def _inline_route(reference: str, routes: dict[str, CommandNode]) -> str:
    route = ""
    for word in reference.split():
        if route and routes[route].is_leaf:
            break
        if word.startswith("-") or re.fullmatch(r"<[^<>\s]+>", word):
            break
        assert re.fullmatch(r"[a-z][a-z-]*", word), f"invalid command token {word}"
        route = f"{route} {word}".strip()
        assert route in routes, f"unknown command route {route}"
    return route


@pytest.mark.parametrize("reference", ["nonexistent", "config_migrate", "doctor migr_ate"])
def test_inline_route_rejects_invalid_command_tokens(reference: str) -> None:
    routes = {node.full_name: node for node in walk_cli()}
    with pytest.raises(AssertionError, match="command"):
        _inline_route(reference, routes)


@pytest.mark.parametrize(
    "reference,expected",
    [("--version", ""), ("doctor <service>", "doctor"), ("sf schema arbitrary_argument", "sf schema")],
)
def test_inline_route_stops_only_at_explicit_boundaries(reference: str, expected: str) -> None:
    routes = {node.full_name: node for node in walk_cli()}
    result = _inline_route(reference, routes)
    assert result == expected


def test_public_inline_command_routes_exist() -> None:
    """Resolve prose command routes without parsing options or invoking callbacks."""
    repo = Path(__file__).parents[1]
    contract = json.loads((repo / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    nodes = walk_cli()
    assert nodes
    routes = {node.full_name: node for node in nodes}
    references = 0
    for document in contract["documents"]:
        text = (repo / document).read_text(encoding="utf-8")
        for match in re.finditer(r"`fieldkit ([^`\n]+)`", text):
            try:
                route = _inline_route(match.group(1), routes)
            except AssertionError as error:
                pytest.fail(f"{document}: {error}")
            references += bool(route)
    assert references > 0


@pytest.mark.parametrize(
    "document,source",
    [
        ("docs/guides/salesforce-auth.md", "src/fieldkit/protected_input.py"),
        ("docs/guides/shadowbot-auth.md", "src/fieldkit/protected_input.py"),
        ("docs/guides/salesforce-auth.md", "src/fieldkit/config/salesforce_cookie.py"),
        ("docs/guides/salesforce-auth.md", "src/fieldkit/config/_integrations.py"),
    ],
)
def test_authentication_guides_track_shared_credential_sources(document: str, source: str) -> None:
    repo = Path(__file__).parents[1]
    contract = json.loads((repo / "docs/documentation-contract.json").read_text(encoding="utf-8"))

    sources = check_documentation_contract._source_files(repo, contract["documents"][document]["sources"])

    assert repo / source in sources


@pytest.mark.parametrize("external", [False, True])
def test_source_files_rejects_symlinked_parent(tmp_path: Path, external: bool) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = (tmp_path if external else repo) / "source-target"
    target.mkdir()
    (target / "module.py").write_text("SENTINEL = 1\n", encoding="utf-8")
    (repo / "linked-source").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="source patterns must not resolve through symlinks"):
        check_documentation_contract._source_files(repo, ["linked-source/*.py"])


def test_block_inventory_includes_nested_list_fences(tmp_path: Path) -> None:
    document = tmp_path / "guide.md"
    document.write_text("- ```console\n  fieldkit --help\n  ```\n", encoding="utf-8")

    result = check_documentation_contract._block_inventory(document)

    assert len(result) == 1
    assert result[0]["language"] == "console"


@pytest.mark.parametrize(
    "content",
    [
        "A | B\n--- | nope\nx | y\n",
        "A | B\n--- | --- | nope\nx | y | z\n",
        "A | B\n-- | --\nx | y\n",
        "A | B\n-- | nope\nx | y\n",
    ],
)
def test_table_inventory_rejects_attempted_malformed_delimiters(tmp_path: Path, content: str) -> None:
    document = tmp_path / "guide.md"
    document.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="Markdown table"):
        check_documentation_contract._table_inventory(document)


@pytest.mark.parametrize("prefix", ["- ", "> "])
def test_fenced_blocks_preserve_raw_binding_and_normalize_container_commands(tmp_path: Path, prefix: str) -> None:
    document = tmp_path / "guide.md"
    indentation = "  " if prefix == "- " else "> "
    source_body = f"{indentation}fieldkit --help\n"
    document.write_text(f"{prefix}```console\n{source_body}{indentation}```\n", encoding="utf-8")

    blocks = check_documentation_contract.fenced_blocks(document)

    assert blocks == [check_documentation_contract.FencedBlock("console", "fieldkit --help\n", source_body)]
    assert check_documentation_contract._block_inventory(document) == [
        {"language": "console", "sha256": hashlib.sha256(f"console\0{source_body}".encode()).hexdigest()}
    ]


@pytest.mark.parametrize(
    "content",
    ["- ```console\n  fieldkit --help\n", "> ```console\n> fieldkit --help\n", "```console\nfieldkit --help\n> ```\n"],
)
def test_fenced_blocks_reject_implicit_container_closing(tmp_path: Path, content: str) -> None:
    document = tmp_path / "guide.md"
    document.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="unclosed fenced block"):
        check_documentation_contract.fenced_blocks(document)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_example_command_registry_requires_exact_automated_owner_set(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    commands = dict(check_documentation_contract.EXAMPLE_COMMANDS)
    if mutation == "extra":
        commands["automated.unowned"] = (("example", "--check"),)
    else:
        commands.pop("automated.exit-code-contract")
    monkeypatch.setattr(check_documentation_contract, "EXAMPLE_COMMANDS", commands)

    with pytest.raises(ValueError, match="automated command registry does not match ownership"):
        check_documentation_contract._validate_example_ownership({"example_verifications": {}}, {})


def test_validate_accepts_complete_current_contract(tmp_path: Path) -> None:
    """A complete contract with current sources and examples is valid."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n\nPlanned work belongs here.\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert findings == ()


def test_validate_rejects_missing_public_document_contract(tmp_path: Path) -> None:
    """Every Markdown document in the public surface needs a contract record."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    del contract["documents"]["README.md"]
    contract["verification"]["first_user_guides_contract"]["paths"].remove("README.md")
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC401", "README.md") in findings


def test_validate_rejects_stale_source_fingerprint(tmp_path: Path) -> None:
    """Implementation changes invalidate the owning document fingerprint."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    raise RuntimeError\n")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC406", "README.md") in findings


def test_refresh_fingerprints_updates_only_selected_reviewed_page(tmp_path: Path) -> None:
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    raise RuntimeError\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'changed'\n")

    check_documentation_contract.refresh_fingerprints(tmp_path, paths=("README.md",))
    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC406", "README.md") not in findings
    assert check_documentation_contract.Finding("DOC406", "ROADMAP.md") in findings


def test_refresh_fingerprints_rejects_unknown_page_without_writing(tmp_path: Path) -> None:
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    original = contract_path.read_bytes()

    with pytest.raises(ValueError, match="unknown document"):
        check_documentation_contract.refresh_fingerprints(tmp_path, paths=("docs/not-registered.md",))

    assert contract_path.read_bytes() == original


def test_refresh_cli_rejects_unscoped_bulk_update(tmp_path: Path) -> None:
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    original = contract_path.read_bytes()

    with pytest.raises(SystemExit) as error:
        check_documentation_contract.main(["--repo-root", str(tmp_path), "--refresh"])

    assert error.value.code == 2
    assert contract_path.read_bytes() == original


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
    write_fixture_file(tmp_path / "README.md", f"# Current behavior\n\nThis is {phrase}.\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC407", "README.md") in findings


@pytest.mark.parametrize(
    "path,text,expected",
    [
        (
            "src/fieldkit/skills/task-management/SKILL.md",
            "In TASKS.md, the local queued section is named `## Backlog`.",
            False,
        ),
        (
            "src/fieldkit/skills/task-sync/SKILL.md",
            "The `Backlog` section in TASKS.md is local and outside the sync markers.",
            False,
        ),
        ("src/fieldkit/skills/task-management/SKILL.md", "Project backlog belongs here.", True),
        ("src/fieldkit/skills/task-management/SKILL.md", "The project `Backlog` contains unreleased work.", True),
        ("src/fieldkit/skills/task-sync/SKILL.md", "The project `Backlog` contains unreleased work.", True),
        (
            "src/fieldkit/skills/task-management/SKILL.md",
            "In TASKS.md, the local queued section is named `## Backlog`. It includes unreleased project work.",
            True,
        ),
        ("src/fieldkit/skills/task-sync/SKILL.md", "We have pending implementation.", True),
        ("README.md", "Use `Backlog` for project work.", True),
    ],
)
def test_task_section_literals_do_not_hide_project_future_work(path: str, text: str, expected: bool) -> None:
    result = check_documentation_contract._has_future_status(path, text)

    assert result is expected


def test_validate_rejects_unresolved_source_pattern(tmp_path: Path) -> None:
    """Source patterns must resolve so missing inputs cannot pass silently."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["sources"] = ["src/missing/**"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC405", "README.md") in findings


def test_validate_rejects_changed_fenced_example(tmp_path: Path) -> None:
    """Edited examples require an explicit reviewed inventory refresh."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit doctor\n```\n")

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC408", "README.md") in findings


def test_validate_rejects_unowned_markdown_table(tmp_path: Path) -> None:
    """A public table cannot bypass the reviewed structured-claim inventory."""
    write_fixture_file(
        tmp_path / "README.md", "# Current behavior\n\n| Name | Value |\n| --- | --- |\n| core | local |\n"
    )
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC410", "README.md") in findings


@pytest.mark.parametrize("fence", ["~~~console\nfieldkit --help\n~~~", " ````console\nfieldkit --help\n ````"])
def test_validate_inventories_commonmark_fence_forms(tmp_path: Path, fence: str) -> None:
    """The block inventory recognizes supported CommonMark fence forms."""
    write_fixture_file(tmp_path / "README.md", f"# Current behavior\n\n{fence}\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    assert check_documentation_contract.validate(tmp_path) == ()


def test_validate_rejects_unclosed_fenced_block(tmp_path: Path) -> None:
    """Malformed unclosed examples fail contract validation."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    check_documentation_contract.refresh_fingerprints(tmp_path)

    with pytest.raises(ValueError, match="unclosed fenced block"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_source_pattern_outside_repository(tmp_path: Path) -> None:
    """Contract source patterns cannot escape the repository."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["sources"] = ["../private.txt"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_unknown_document_field(tmp_path: Path) -> None:
    """Unknown document metadata fails the closed contract schema."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["unvalidated"] = True
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="Additional properties"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_document_without_verification_owner(tmp_path: Path) -> None:
    """Each public document has exactly one declared verification owner."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["verification"]["first_user_guides_contract"]["paths"] = []
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one verification owner"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_duplicate_json_key(tmp_path: Path) -> None:
    """Duplicate JSON keys cannot ambiguously override contract values."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    text = contract_path.read_text(encoding="utf-8").replace(
        '"schema_version": 2,', '"schema_version": 2, "schema_version": 2,', 1
    )
    contract_path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_source_removed_from_public_tree(tmp_path: Path) -> None:
    """Public documents cannot depend on sources excluded from the export."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path, excluded=["src/fieldkit/**"])
    check_documentation_contract.refresh_fingerprints(tmp_path)
    check_documentation_contract.refresh_block_inventory(tmp_path)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC409", "README.md") in findings


def test_validate_rejects_schema_version_one(tmp_path: Path) -> None:
    """The ownership-aware contract cannot silently fall back to hash-only schema v1."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["schema_version"] = 1
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_duplicate_fenced_block_identifier(tmp_path: Path) -> None:
    """Stable example identifiers are unique across the complete public corpus."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n\n```text\nLater\n```\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    duplicate = contract["documents"]["README.md"]["fenced_blocks"][0]["id"]
    contract["documents"]["ROADMAP.md"]["fenced_blocks"][0]["id"] = duplicate
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="identifiers must be unique"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_incompatible_example_verification(tmp_path: Path) -> None:
    """A manual classification cannot point at an automated scenario."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["fenced_blocks"][0]["classification"] = "credentialed_manual_integration"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="incompatible verification ownership"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_arbitrary_example_verification_route(tmp_path: Path) -> None:
    """Contract authors cannot declare an unenforced route and call it verification."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["example_verifications"]["automated.generated-reference"]["evidence"] = "trust me"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported example verification route"):
        check_documentation_contract.validate(tmp_path)


def test_validate_rejects_missing_example_verification_evidence(tmp_path: Path) -> None:
    """Registered evidence must exist instead of remaining a future filename."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    (tmp_path / "docs/release-readiness/rehearsal-evidence.schema.json").unlink()

    with pytest.raises(ValueError, match="example verification evidence is unavailable"):
        check_documentation_contract.validate(tmp_path)


def test_refresh_blocks_rejects_missing_ownership(tmp_path: Path) -> None:
    """Refreshing hashes cannot manufacture ownership for a newly added example."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n\n```console\nfieldkit --help\n```\n")

    with pytest.raises(ValueError, match="ownership must be reviewed"):
        check_documentation_contract.refresh_block_inventory(tmp_path)


def test_refresh_blocks_rejects_reordered_ownership(tmp_path: Path) -> None:
    """Reordering same-count blocks cannot transfer safe ownership to different content."""
    write_fixture_file(
        tmp_path / "README.md",
        "# Current behavior\n\n```console\nfieldkit --help\n```\n\n```console\nfieldkit doctor\n```\n",
    )
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    write_fixture_file(
        tmp_path / "README.md",
        "# Current behavior\n\n```console\nfieldkit doctor\n```\n\n```console\nfieldkit --help\n```\n",
    )

    with pytest.raises(ValueError, match="explicit ownership review"):
        check_documentation_contract.refresh_block_inventory(tmp_path)


@pytest.mark.parametrize(
    "document",
    [
        "src/README.md",
        "src/fieldkit/ingest/AGENTS.md",
        "src/fieldkit/skills/example/SKILL.md",
        "src/fieldkit/skills/example/references/detail.md",
    ],
)
def test_public_policy_requires_source_document_ownership(tmp_path: Path, document: str) -> None:
    """Exported source prose cannot disappear from documentation ownership checks."""
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_fixture_file(tmp_path / "src/fieldkit/__main__.py", "def main() -> None:\n    pass\n")
    write_fixture_file(tmp_path / "pyproject.toml", "[project]\nname = 'example'\n")
    write_contract_validation_fixture(tmp_path)
    write_fixture_file(tmp_path / document, "# Public instructions\n")
    policy_path = Path("docs/release-readiness/public-surface-policy.json")
    policy = (Path(__file__).parents[1] / policy_path).read_text(encoding="utf-8")
    write_fixture_file(tmp_path / policy_path, policy)

    findings = check_documentation_contract.validate(tmp_path)

    assert check_documentation_contract.Finding("DOC401", document) in findings


@pytest.mark.parametrize(
    "index",
    [
        None,
        "",
        "- [one](one/SKILL.md)\n- [one](one/SKILL.md)\n",
        "- [one](../outside.md)\n",
        "- [two](two/SKILL.md)\n",
        "- [one](one/SKILL.md)\n- [extra](missing/SKILL.md) trailing text\n",
        "- [one](one/SKILL.md)\n* [extra](missing/SKILL.md)\n",
        "## Elsewhere\n- [one](one/SKILL.md)\n",
        "- [one](one/SKILL.md)\n## Skill index\n",
        "- [one](one/SKILL.md)\n## Skill index \n",
        "- [one](one/SKILL.md)\n## Skill index ##\n",
        "- [one](one/SKILL.md)\n  ## Skill index\n",
        "- [one](one/SKILL.md)\n",
    ],
)
def test_skill_index_matches_packaged_entrypoints(tmp_path: Path, index: str | None) -> None:
    write_fixture_file(tmp_path / "README.md", "# Current behavior\n")
    write_fixture_file(tmp_path / "ROADMAP.md", "# Roadmap\n")
    write_contract_validation_fixture(tmp_path)
    write_fixture_file(tmp_path / "src/fieldkit/skills/one/SKILL.md", "# One\n")
    if index is not None:
        write_fixture_file(
            tmp_path / "src/fieldkit/skills/README.md", f"# Skills\n\n## Skill index\n\n{index}\n## Contributing\n"
        )

    findings = check_documentation_contract.validate(tmp_path)

    index_findings = [finding for finding in findings if finding.criterion_id == "DOC411"]
    expected = (
        []
        if index == "- [one](one/SKILL.md)\n"
        else [check_documentation_contract.Finding("DOC411", "src/fieldkit/skills/README.md")]
    )
    assert index_findings == expected


def test_main_writes_contract_errors_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Contract-load failures use stderr and the stable error status."""
    result = check_documentation_contract.main(["--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "Documentation contract: ERROR" in captured.err
