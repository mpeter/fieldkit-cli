"""Conservative pytest selection for checkout-only hook and script tests."""

import ast
import re
from pathlib import Path

import pytest

_WORD = re.compile(r"[A-Za-z_]\w*")


def _pytest_plugin_names(source: str) -> set[str]:
    """Resolve static plugin registrations and their local assignment aliases."""
    bindings: dict[str, list[ast.expr]] = {}
    additions: list[ast.expr] = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "pytest_plugins"
        ):
            additions.append(node.value)
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "pytest_plugins"
            and node.value.func.attr in {"append", "extend"}
        ):
            additions.extend(node.value.args)
    pending = list(bindings.get("pytest_plugins", []))
    pending.extend(additions)
    visited: set[str] = set()
    names: set[str] = set()
    while pending:
        for expression in ast.walk(pending.pop()):
            if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                names.update(name.strip() for name in expression.value.split(",") if name.strip())
            elif isinstance(expression, ast.Name) and expression.id in bindings and expression.id not in visited:
                visited.add(expression.id)
                pending.extend(bindings[expression.id])
    return names


def _fixture_tool_names(source: str, tool_names: set[str]) -> tuple[set[str], bool]:
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
                autouse |= node.name.startswith("pytest_runtest_") or node.name in {
                    "pytest_pyfunc_call",
                    "pytest_fixture_setup",
                    "pytest_fixture_post_finalizer",
                }
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
    fixture_scopes = {path: path.parent for path in sources if path.name == "conftest.py"}
    pending = list(sources)
    while pending:
        owner = pending.pop()
        if owner in fixture_scopes:
            for node in ast.walk(ast.parse(sources[owner])):
                if not isinstance(node, (ast.ImportFrom, ast.Import)):
                    continue
                parents = [root / "src", root, owner.parent]
                names = [alias.name for alias in node.names if alias.name != "*"]
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        parents = [owner.parents[node.level - 1]]
                    if node.module:
                        names = [node.module, *(f"{node.module}.{name}" for name in names)]
                for parent in parents:
                    for name in names:
                        # Conftest's production dependencies are selected by Tach.
                        if owner.name == "conftest.py" and name.split(".")[0] == "fieldkit":
                            continue
                        module = parent.joinpath(*name.split("."))
                        for candidate in (module.with_suffix(".py"), module / "__init__.py"):
                            path = candidate.resolve()
                            if path.is_relative_to(root.resolve()) and path.is_file() and path not in fixture_scopes:
                                sources[path] = path.read_text(encoding="utf-8")
                                fixture_scopes[path] = fixture_scopes[owner]
                                pending.append(path)
        for name in _pytest_plugin_names(sources[owner]):
            for import_root in (root / "src", root, owner.parent):
                module = import_root.joinpath(*name.split("."))
                for candidate in (module.with_suffix(".py"), module / "__init__.py"):
                    path = candidate.resolve()
                    if path.is_relative_to(root.resolve()) and path.is_file() and path not in fixture_scopes:
                        sources[path] = path.read_text(encoding="utf-8")
                        fixture_scopes[path] = (root / "tests").resolve()
                        pending.append(path)
    references = {path: set(_WORD.findall(source)) for path, source in sources.items()}
    selected: set[Path] = set()
    while True:
        autouse_paths: set[Path] = set()
        for path, scope in fixture_scopes.items():
            fixture_names, autouse = _fixture_tool_names(sources[path], tool_names)
            tool_names.update(fixture_names)
            if autouse:
                autouse_paths.update(test for test in references if test.is_relative_to(scope))
        for path in selected - fixture_scopes.keys():
            if path.name.startswith("test_"):
                continue
            fixture_names, autouse = _fixture_tool_names(sources[path], tool_names)
            tool_names.update(fixture_names)
            if autouse:
                autouse_paths.update(references)
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
