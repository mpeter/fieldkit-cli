"""Tests for the public dependency-map generator."""

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
