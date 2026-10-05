"""Regression contracts for deterministic production-code impact selection."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from tach.extension import TachPytestPluginHandler
from tach.parsing import parse_project_config

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SELECTION_TIMEOUT_SECONDS = 60


def _selected_test_files(module: str) -> set[str]:
    config = parse_project_config(root=_ROOT)
    assert config is not None
    changed = _ROOT / "src/fieldkit" / module
    handler = TachPytestPluginHandler(_ROOT, config, [changed], {changed})
    return {
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / "tests").rglob("test_*.py")
        if not handler.should_remove_items(path.resolve())
    }


@pytest.mark.parametrize("foundation", ["errors.py", "config/__init__.py"])
def test_foundation_change_selects_dependent_tests(foundation: str) -> None:
    result = _selected_test_files(foundation)
    dependent = _selected_test_files("llm/__init__.py")

    assert result >= dependent
    assert "tests/test_llm.py" in dependent


@pytest.mark.integration
@pytest.mark.parametrize("process", range(4))
def test_separate_processes_select_identical_tests(process: int) -> None:
    probe = """
import json
from pathlib import Path
from tach.extension import TachPytestPluginHandler
from tach.parsing import parse_project_config
root = Path.cwd()
changed = root / "src/fieldkit/errors.py"
handler = TachPytestPluginHandler(root, parse_project_config(root=root), [changed], {changed})
print(json.dumps(sorted(
    p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py")
    if not handler.should_remove_items(p.resolve())
)))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SELECTION_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, f"process {process}: {result.stderr}"
    assert set(json.loads(result.stdout)) == _selected_test_files("errors.py")


def test_tach_source_roots_do_not_overlap() -> None:
    config = tomllib.loads((_ROOT / "tach.toml").read_text(encoding="utf-8"))
    roots = [(_ROOT / root).resolve() for root in config["source_roots"]]

    assert len(roots) == len(set(roots))
    assert not any(left.is_relative_to(right) for left in roots for right in roots if left != right)
    assert {root.relative_to(_ROOT).as_posix() for root in roots} == {"src", "tests"}


def test_tach_retains_all_production_boundaries() -> None:
    config = tomllib.loads((_ROOT / "tach.toml").read_text(encoding="utf-8"))
    modules = {module["path"] for module in config["modules"]}

    assert "fieldkit.errors" in modules
    assert "fieldkit.config" in modules
    assert "fieldkit.commands" in modules
    assert "hooks" not in modules
