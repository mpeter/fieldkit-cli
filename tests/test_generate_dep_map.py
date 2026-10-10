"""Tests for the public dependency-map generator."""

from pathlib import Path

import pytest

from scripts import generate_dep_map

pytestmark = pytest.mark.unit


def test_public_dependency_map_excludes_operator_specific_mcp_registry() -> None:
    """The exported reference contains portable codebase facts only."""
    generated = generate_dep_map.generate()

    assert "MCP Groups" not in generated
    assert "mcpjungle" not in generated
    assert "<fieldkit-mcp-dir>" not in generated
    assert "fieldkit-dataverse" not in generated


def _architecture_tree(generated: str) -> list[str]:
    section = generated.split("## Architecture Layers", 1)[1]
    return section.split("```", 2)[1].splitlines()


def _package_dirs(root: Path) -> set[str]:
    return {
        child.name for child in root.iterdir() if (child / "__init__.py").is_file() and not child.name.startswith("_")
    }


def test_architecture_tree_lists_exactly_the_current_packages() -> None:
    """The literal tree tracks the package layout instead of drifting from it."""
    package_root = Path(generate_dep_map.__file__).resolve().parents[1] / "src" / "fieldkit"
    tree = _architecture_tree(generate_dep_map.generate())

    top_level = {line.split()[0].rstrip("/") for line in tree if line.startswith("  ") and not line.startswith("    ")}
    commands = {line.split()[0].rstrip("/") for line in tree if line.startswith("    ")}

    assert top_level == _package_dirs(package_root) | {"skills"}
    assert commands == _package_dirs(package_root / "commands")
