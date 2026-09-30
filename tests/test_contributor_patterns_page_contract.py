"""Keep contributor pattern entry points tied to live source and regressions."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

pytestmark = pytest.mark.integration

PAGE = Path("docs/patterns/README.md")
MODULE_ENTRY_POINTS = {
    "src/fieldkit/ingest/prepared.py": ("save_prepared", "complete_prepared"),
    "src/fieldkit/ingest/replay.py": ("replay_prepared",),
    "src/fieldkit/util/owned_markdown.py": ("read_owned_markers",),
    "src/fieldkit/util/workspace_paths.py": ("resolve_workspace_output",),
}
BEHAVIOR_SELECTORS = (
    "tests/test_ingest_prepared_store.py::test_replay_failure_retains_intent",
    "tests/test_ingest_run_lock.py::test_busy_run_has_no_database_or_discovery_effects",
    "tests/test_owned_markdown.py::test_non_owned_examples_are_excluded",
    "tests/test_text_snapshot.py::test_snapshot_refuses_change_during_read",
    "tests/test_atomic_text_create.py::test_atomic_create_refuses_existing_destination",
)


def test_patterns_page_has_actionable_current_structure() -> None:
    page = PAGE.read_text(encoding="utf-8")
    tokens = MarkdownIt("commonmark").parse(page)
    headings = [tokens[index + 1].content for index, token in enumerate(tokens) if token.type == "heading_open"]
    assert headings == ["Contributor patterns"]
    assert sum(token.type == "list_item_open" for token in tokens) == 8
    assert "When adding a command or a file-producing workflow" in page
    assert "Run the cited\ntest" in page
    assert "Fieldkit" not in page
    assert "FieldKit" not in page
    assert "compatibility shim" not in page


@pytest.mark.parametrize("path,functions", MODULE_ENTRY_POINTS.items())
def test_pattern_entry_points_exist_in_current_source(path: str, functions: tuple[str, ...]) -> None:
    page = PAGE.read_text(encoding="utf-8")
    source = Path(path)
    assert source.is_file()
    module = source.with_suffix("").as_posix().removeprefix("src/").replace("/", ".")
    assert module in page
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definitions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(functions) <= definitions


@pytest.mark.parametrize("selector", BEHAVIOR_SELECTORS)
def test_cited_behavior_selector_exists(selector: str) -> None:
    assert selector in PAGE.read_text(encoding="utf-8")
    path, name = selector.split("::")
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    assert name in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_all_source_and_test_references_are_live() -> None:
    page = PAGE.read_text(encoding="utf-8")
    paths = set(re.findall(r"(?:src/fieldkit|tests)/[\w./-]+(?:\.py|/)", page))
    assert paths
    for path in paths:
        assert Path(path).exists(), path
