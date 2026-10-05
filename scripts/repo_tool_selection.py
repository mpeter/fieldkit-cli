"""Conservative pytest selection for checkout-only hook and script tests."""

import ast
import re
from pathlib import Path

import pytest

_WORD = re.compile(r"[A-Za-z_]\w*")


def _conftest_tool_names(source: str, tool_names: set[str]) -> tuple[set[str], bool]:
    """Follow imported aliases and helper calls into fixture identifiers."""
    tree = ast.parse(source)
    names = set(tool_names)
    autouse = False
    definitions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    while True:
        previous = set(names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if set(_WORD.findall(alias.name)) & names:
                        names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and (
                set(_WORD.findall(node.module or "")) & names or any(alias.name in names for alias in node.names)
            ):
                names.update(alias.asname or alias.name for alias in node.names)
        for node in tree.body:
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and set(_WORD.findall(ast.get_source_segment(source, node.value) or "")) & names
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(
                    target.id for binding in targets for target in ast.walk(binding) if isinstance(target, ast.Name)
                )
        for node in definitions:
            if set(_WORD.findall(ast.get_source_segment(source, node) or "")) & names:
                names.add(node.name)
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        autouse |= any(
                            keyword.arg == "autouse"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                            for keyword in decorator.keywords
                        )
                        names.update(
                            keyword.value.value
                            for keyword in decorator.keywords
                            if keyword.arg == "name"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)
                        )
        if names == previous:
            return names - tool_names, autouse


def repo_tool_test_paths(root: Path) -> set[Path]:
    """Find test files referencing tools, including through local test helpers.

    Source text includes imports, dynamic import names, subprocess paths and
    documentation. Ambiguous names deliberately select extra tests rather than
    risk excluding a tool's tests. The inventory is rebuilt for each collection.
    """
    tool_names = {"hooks", "scripts"}
    for directory in (root / "hooks", root / "scripts"):
        for path in directory.rglob("*.py"):
            tool_names.add(path.relative_to(directory).parts[0].removesuffix(".py"))
    tool_names.discard("__init__")
    sources = {path.resolve(): path.read_text(encoding="utf-8") for path in (root / "tests").rglob("*.py")}
    references = {path: set(_WORD.findall(source)) for path, source in sources.items()}
    selected: set[Path] = set()
    while True:
        autouse_paths: set[Path] = set()
        for path, source in sources.items():
            if path.name == "conftest.py":
                fixture_names, autouse = _conftest_tool_names(source, tool_names)
                tool_names.update(fixture_names)
                if autouse:
                    autouse_paths.update(test for test in references if test.is_relative_to(path.parent))
        added = {
            path
            for path, names in references.items()
            if path not in selected and (names & tool_names or path in autouse_paths)
        }
        if not added:
            return selected
        selected.update(added)
        tool_names.update(
            path.parent.name if path.stem == "__init__" else path.stem for path in added if path.stem != "conftest"
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--repo-tools-only",
        action="store_true",
        help="Run checkout hook/script tests independently of Tach impact selection.",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--repo-tools-only") and config.pluginmanager.hasplugin("tach"):
        raise pytest.UsageError("--repo-tools-only requires -p no:tach")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--repo-tools-only"):
        return
    paths = repo_tool_test_paths(config.rootpath)
    kept = [item for item in items if item.path.resolve() in paths]
    deselected = [item for item in items if item.path.resolve() not in paths]
    items[:] = kept
    config.hook.pytest_deselected(items=deselected)
