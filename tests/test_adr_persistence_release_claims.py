"""Executable contracts for the persistence and public-release decisions."""

import ast
from pathlib import Path

import pytest

from fieldkit.util.atomic import locked_json_update
from scripts import release_check

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _document(name: str) -> str:
    return (ROOT / "docs/adr" / name).read_text(encoding="utf-8")


def _function(path: str, name: str) -> ast.FunctionDef:
    module = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_locked_json_update_publishes_complete_incremental_changes(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    with locked_json_update(target) as state:
        state["first"] = 1
    first_publication = target.read_text(encoding="utf-8")

    with locked_json_update(target) as state:
        assert state == {"first": 1}
        state["second"] = 2

    assert '"second": 2' in target.read_text(encoding="utf-8")
    assert '"second"' not in first_publication
    assert target.with_suffix(".json.lock").is_file()
    assert "cooperating" in _document("0004-safe-persistence.md")


def test_ingest_replay_completes_only_after_all_file_effects() -> None:
    replay = _function("src/fieldkit/ingest/replay.py", "replay_prepared")
    calls = [
        node.func.id
        for statement in replay.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.index("publish_prepared_note") < calls.index("publish_prepared_pursuit")
    assert calls.index("publish_prepared_pursuit") < calls.index("publish_prepared_tasks")
    assert calls.index("publish_prepared_tasks") < calls.index("complete_prepared")
    document = _document("0004-safe-persistence.md")
    assert "journal" in document and "do not roll back" in document


def test_release_report_keeps_unproven_criteria_pending() -> None:
    criterion = release_check.release_manual_evidence.Criterion
    pending = criterion("cutover-approval", "pending", "independent approval required")
    report = release_check.Report(1, "pending", "a" * 40, (pending,))

    assert report.to_dict()["status"] == "pending"
    assert report.to_dict()["criteria"] == [
        {"id": "cutover-approval", "status": "pending", "evidence": "independent approval required"}
    ]
    document = _document("0005-verifiable-public-release.md")
    assert "pending" in document and "independent behavioral evidence" in document


def test_release_check_requires_all_criteria_to_pass() -> None:
    main = _function("scripts/release_check.py", "main")
    conditions = [
        node.test for node in ast.walk(main) if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Call)
    ]

    assert any(
        isinstance(condition.func, ast.Name)
        and condition.func.id == "all"
        and "criterion.status == 'pass'" in ast.unparse(condition)
        for condition in conditions
    )
    assert "mixed-revision evidence remains non-passing" in _document("0005-verifiable-public-release.md")
