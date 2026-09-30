"""Keep watcher test isolation complete without allocating recording mocks."""

import ast
import importlib
from pathlib import Path

import conftest
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    [
        "status",
        "waiting_on_tracker",
        "close_date_countdown",
        "draft_queue",
        "morning_brief",
        "contract_expiry",
        "backstory_health",
        "slack_threads",
        "pursuit_stalls",
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_status_writers_are_isolated(name: str, dry_run: bool, tmp_path: Path) -> None:
    """Every existing watcher alias uses the same non-writing callable."""
    module = importlib.import_module(f"fieldkit.watch.{name}")
    before = list(tmp_path.iterdir())
    result = module.write_run_status(
        watcher="run-all",
        outcome="ok",
        records_checked=0,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=0.0,
        dry_run=dry_run,
    )
    assert result == ("skipped" if dry_run else "written")
    assert module.write_run_status is conftest._discard_run_status
    assert list(tmp_path.iterdir()) == before


def test_status_writer_inventory_covers_importing_watchers() -> None:
    """A new watcher alias cannot silently bypass the isolation inventory."""
    root = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "watch"
    expected = {"fieldkit.watch.status"}
    for path in root.glob("*.py"):
        if path.name == "__init__.py":
            continue  # Package re-exports do not execute watcher writes.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "fieldkit.watch.status"
            and any(alias.name == "write_run_status" for alias in node.names)
            for node in ast.walk(tree)
        ):
            expected.add(f"fieldkit.watch.{path.stem}")
    actual = {module.__name__ for module in conftest._STATUS_WRITER_MODULES}
    assert actual == expected


def test_status_read_helpers_remain_isolated(tmp_path: Path) -> None:
    """Default status checks never skip work or resolve the operator's home."""
    status = importlib.import_module("fieldkit.watch.status")
    result = status.was_run_today("fixture-only")
    assert result is False
    assert status.get_fieldkit_home() == tmp_path
