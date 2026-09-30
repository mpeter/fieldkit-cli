"""Structure checks for the three public architecture decisions."""

import ast
from pathlib import Path

import pytest

from scripts.documentation_commands import DOCUMENT_COMMANDS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("name", "claims"),
    [
        (
            "0001-local-first-roots.md",
            ("get_fieldkit_root()", "get_fieldkit_home()", "get_fieldkit_data()", "workspace's `data` directory"),
        ),
        (
            "0002-optional-integrations.md",
            ("base dependency set", "on selection", "Missing packages and authentication", "local\nor no-LLM path"),
        ),
        (
            "0003-thin-command-adapters.md",
            ("fieldkit.brief", "fieldkit.pipeline", "commands to domains", "not a claim that every existing command"),
        ),
    ],
)
def test_architecture_decision_claims_match_owned_sections(name: str, claims: tuple[str, ...]) -> None:
    document = (ROOT / "docs/adr" / name).read_text(encoding="utf-8")

    assert document.count("## Decision") == 1
    assert document.count("## Consequences") == 1
    for claim in claims:
        assert claim in document


def test_architecture_decisions_have_one_fixed_owner_command() -> None:
    assert DOCUMENT_COMMANDS["adr_architecture_contract"] == (
        ("uv", "run", "pytest", "tests/test_adr_architecture_claims.py", "-q", "-n", "0"),
    )


def _module(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def _imports(module: ast.Module) -> set[str]:
    return {node.module for node in ast.walk(module) if isinstance(node, ast.ImportFrom) and node.module is not None}


def _definitions(module: ast.Module) -> set[str]:
    return {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


def test_root_accessors_keep_code_workspace_and_data_responsibilities() -> None:
    module = _module("src/fieldkit/config/_paths.py")

    assert {"get_fieldkit_root", "get_fieldkit_home", "get_fieldkit_data"} <= _definitions(module)
    assert "fieldkit.config.source" in _imports(module)
    data_accessor = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_get_fieldkit_data_from_config"
    )
    assert ast.unparse(data_accessor.body[-1]) == "return get_fieldkit_home() / 'data'"


def test_optional_commands_load_after_selection() -> None:
    dispatcher = _module("src/fieldkit/__main__.py")
    lazy_group = next(node for node in dispatcher.body if isinstance(node, ast.ClassDef) and node.name == "_LazyGroup")
    get_command = next(
        node for node in lazy_group.body if isinstance(node, ast.FunctionDef) and node.name == "get_command"
    )

    assert "importlib.import_module(module_path)" in ast.unparse(get_command)
    assert "fieldkit.config.optional_dependencies" in _imports(dispatcher)
    assert not any(
        node.module and node.module.startswith("fieldkit.commands.")
        for node in dispatcher.body
        if isinstance(node, ast.ImportFrom)
    )


@pytest.mark.parametrize(
    ("adapter", "domain"),
    [
        ("src/fieldkit/commands/brief/cli.py", "fieldkit.brief.pipeline_only"),
        ("src/fieldkit/commands/brief/generate.py", "fieldkit.brief.merged"),
        ("src/fieldkit/commands/pipeline/cli.py", "fieldkit.pipeline.main"),
    ],
)
def test_brief_and_pipeline_adapters_import_their_domain(adapter: str, domain: str) -> None:
    adapter_imports = _imports(_module(adapter))
    domain_imports = _imports(_module("src/" + domain.replace(".", "/") + ".py"))

    assert domain in adapter_imports
    assert not any(name.startswith("fieldkit.commands.") for name in domain_imports)
