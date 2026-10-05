"""Conservative pytest selection for checkout-only hook and script tests."""

import re
from pathlib import Path

import pytest

_WORD = re.compile(r"[A-Za-z_]\w*")


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
    references = {
        path.resolve(): set(_WORD.findall(path.read_text(encoding="utf-8"))) for path in (root / "tests").rglob("*.py")
    }
    selected: set[Path] = set()
    while added := {path for path, names in references.items() if path not in selected and names & tool_names}:
        selected.update(added)
        tool_names.update(
            path.parent.name if path.stem == "__init__" else path.stem for path in added if path.stem != "conftest"
        )
    return selected


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
