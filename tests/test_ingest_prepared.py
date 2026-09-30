"""Strict prepared-output records retain exact decisions without file effects."""

import hashlib
import json
from pathlib import Path

import pytest

from fieldkit.ingest.paths import compute_vault_path
from fieldkit.ingest.prepared import ReplayIntent, decode_prepared, resolve_prepared_path
from fieldkit.ingest.task_effect import publish_prepared_tasks
from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect, prepare_effect

pytestmark = pytest.mark.unit


def test_schema_v1_golden_record_retains_its_destination() -> None:
    """A future allocator change must not invalidate already-journaled v1 output."""
    path = (
        "accounts/acme/meetings/2026-09-27-acme-meeting-"
        "ffa6a744d78d28386438b4a8e8ee32f56718633735057b1b5ddfb5d224d45d98.md"
    )
    raw = json.dumps(
        {
            "schema_version": 1,
            "pipeline_id": "transcript-ingest",
            "source_id": "source-1",
            "pipeline_version": "0.1.0",
            "vault_relative_path": path,
            "note_content": "Meeting",
            "note_sha256": hashlib.sha256(b"Meeting").hexdigest(),
            "account": "acme",
            "meeting_date": "2026-09-27",
            "meeting_title": "Acme meeting",
            "pursuits": [],
            "action_items": [],
            "tasks": [],
            "degraded": False,
        }
    )
    prepared = decode_prepared(raw)
    assert prepared.vault_relative_path == path
    assert decode_prepared(prepared.model_dump_json()) == prepared


def _payload() -> dict[str, object]:
    note = "---\nsource_id: source-1\n---\nMeeting\n"
    return {
        "schema_version": 1,
        "pipeline_id": "transcript-ingest",
        "source_id": "source-1",
        "pipeline_version": "0.1.0",
        "vault_relative_path": compute_vault_path(
            Path(), "acme", "2026-09-27", "Acme meeting", source_id="source-1"
        ).as_posix(),
        "note_content": note,
        "note_sha256": hashlib.sha256(note.encode("utf-8")).hexdigest(),
        "account": "acme",
        "meeting_date": "2026-09-27",
        "meeting_title": "Acme meeting",
        "pursuits": ["project"],
        "action_items": ["Send proposal"],
        "tasks": [
            {
                "text": "Send proposal",
                "cls": "my_task",
                "owner": "Alice",
                "rationale": "Assigned",
                "pursuit_label": "Acme",
            }
        ],
        "degraded": False,
    }


@pytest.mark.parametrize("note", ["   ", "```\nunclosed code", "<!-- unclosed comment"])
def test_prepared_record_rejects_unpublishable_note(note: str) -> None:
    payload = _payload()
    payload["note_content"] = note
    payload["note_sha256"] = hashlib.sha256(note.encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="Invalid prepared"):
        decode_prepared(json.dumps(payload))


@pytest.mark.parametrize("text", [" ", "two\nlines", "fieldkit-task:forged", "<!-- forged -->"])
def test_prepared_record_rejects_unpublishable_task(text: str) -> None:
    payload = _payload()
    payload["action_items"] = [text]
    payload["tasks"] = [{"text": text, "cls": "my_task", "owner": "Alice", "rationale": "", "pursuit_label": "Acme"}]
    with pytest.raises(ValueError, match="Invalid prepared"):
        decode_prepared(json.dumps(payload))


@pytest.mark.parametrize("title", [" ", "two\nlines", "hidden\u2028break"])
def test_prepared_record_rejects_unpublishable_pursuit_title(title: str) -> None:
    payload = _payload()
    payload["meeting_title"] = title
    payload["tasks"] = []
    payload["action_items"] = []
    payload["vault_relative_path"] = compute_vault_path(
        Path(), "acme", "2026-09-27", title, source_id="source-1"
    ).as_posix()
    with pytest.raises(ValueError, match="Invalid prepared"):
        decode_prepared(json.dumps(payload))


def test_prepared_output_round_trip_preserves_classified_tasks(tmp_path: Path) -> None:
    prepared = decode_prepared(json.dumps(_payload()))
    assert prepared.source_id == "source-1"
    assert prepared.tasks[0].text == "Send proposal"
    assert decode_prepared(prepared.model_dump_json()) == prepared
    assert resolve_prepared_path(prepared, tmp_path) == tmp_path / prepared.vault_relative_path
    assert list(tmp_path.iterdir()) == []


def test_prepared_task_replay_preserves_original_positions_and_edits(tmp_path: Path) -> None:
    payload = _payload()
    payload["action_items"] = ["Ignore", "Send proposal"]
    payload["tasks"] = [
        {"text": "Ignore", "cls": "drop", "owner": "", "rationale": "", "pursuit_label": ""},
        {"text": "Send proposal", "cls": "my_task", "owner": "Alice", "rationale": "Assigned", "pursuit_label": "Acme"},
    ]
    prepared = decode_prepared(json.dumps(payload))
    assert publish_prepared_tasks(ReplayIntent(prepared), tmp_path) == (1, 0)
    expected = prepare_effect(
        TaskEffect("source-1", 1, ClassifiedItem("Send proposal", ItemClass.MY_TASK, "Alice", "Assigned", "Acme")),
        meeting_date=prepared.meeting_date,
        meeting_title=prepared.meeting_title,
    )
    path = tmp_path / "TASKS.md"
    original = path.read_text(encoding="utf-8")
    assert expected.identity in original
    edited = original.replace("Send proposal", "Human revision")
    path.write_text(edited, encoding="utf-8")
    assert publish_prepared_tasks(ReplayIntent(prepared), tmp_path) == (0, 0)
    assert path.read_text(encoding="utf-8") == edited


def test_prepared_tasks_refuse_symlink_redirection(tmp_path: Path) -> None:
    prepared = decode_prepared(json.dumps(_payload()))
    target = tmp_path / "other.md"
    target.write_text("untouched", encoding="utf-8")
    (tmp_path / "TASKS.md").symlink_to(target)
    with pytest.raises(ValueError, match="redirects its authorized destination"):
        publish_prepared_tasks(ReplayIntent(prepared), tmp_path)
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("action_items", [[], ["Different task"], ["Send proposal", "Another task"]])
def test_prepared_output_rejects_misaligned_task_decisions(action_items: list[str]) -> None:
    payload = _payload()
    payload["action_items"] = action_items
    with pytest.raises(ValueError, match=r"^Invalid prepared ingest output$"):
        decode_prepared(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("pipeline_id", "another-pipeline"),
        ("degraded", "false"),
        ("note_sha256", "0" * 64),
        ("vault_relative_path", "/private/note.md"),
        ("vault_relative_path", "accounts/../note.md"),
        ("vault_relative_path", "accounts\\note.md"),
        ("vault_relative_path", "accounts//note.md"),
        ("vault_relative_path", "TASKS.md"),
        ("vault_relative_path", ".git/config"),
        ("vault_relative_path", "accounts/other/meetings/meeting.md"),
        ("vault_relative_path", "accounts/acme/pursuits/meeting.md"),
        ("vault_relative_path", "accounts/acme/meetings/.hidden.md"),
        ("vault_relative_path", "accounts/acme/meetings/meeting\n.md"),
        ("vault_relative_path", "accounts/acme/meetings/meeting.txt"),
        ("meeting_date", "2026-02-30"),
        ("meeting_date", "2026-09-28"),
        ("meeting_title", "Different meeting"),
        ("source_id", "source-2"),
        ("extra", "private-payload"),
    ],
)
def test_prepared_output_rejects_invalid_payload(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ValueError, match="Invalid prepared ingest output") as caught:
        decode_prepared(json.dumps(payload))
    assert "private-payload" not in str(caught.value)


@pytest.mark.parametrize("pursuits", [["project", "project"], ["first", "second", "first"], ["Project", "project"]])
def test_prepared_output_rejects_duplicate_pursuit_targets(pursuits: list[str]) -> None:
    payload = _payload()
    payload["pursuits"] = pursuits
    with pytest.raises(ValueError, match=r"^Invalid prepared ingest output$"):
        decode_prepared(json.dumps(payload))


def test_prepared_output_preserves_distinct_pursuit_order() -> None:
    payload = _payload()
    payload["pursuits"] = ["second", "first"]
    prepared = decode_prepared(json.dumps(payload))
    assert prepared.pursuits == ("second", "first")


def test_prepared_output_rejects_duplicate_json_keys() -> None:
    raw = json.dumps(_payload()).replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
    with pytest.raises(ValueError, match="Invalid prepared ingest output"):
        decode_prepared(raw)


def test_prepared_path_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "accounts").symlink_to(outside, target_is_directory=True)
    prepared = decode_prepared(json.dumps(_payload()))
    with pytest.raises(ValueError, match="Prepared output escapes workspace"):
        resolve_prepared_path(prepared, workspace)
    assert list(outside.iterdir()) == []


def test_prepared_output_rejects_deep_json_without_native_error() -> None:
    with pytest.raises(ValueError, match=r"^Invalid prepared ingest output$"):
        decode_prepared("[" * 1100 + "0" + "]" * 1100)


@pytest.mark.parametrize("failure", ["missing", "loop"])
def test_prepared_path_hides_resolution_errors(tmp_path: Path, failure: str) -> None:
    workspace = tmp_path / "private-workspace"
    if failure == "loop":
        workspace.symlink_to(workspace)
    prepared = decode_prepared(json.dumps(_payload()))
    with pytest.raises(ValueError, match=r"^Cannot resolve prepared output path$") as caught:
        resolve_prepared_path(prepared, workspace)
    assert str(tmp_path) not in str(caught.value)


def test_prepared_path_rejects_leaf_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "accounts/acme/meetings"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("untouched", encoding="utf-8")
    prepared = decode_prepared(json.dumps(_payload()))
    (workspace / prepared.vault_relative_path).symlink_to(outside)
    with pytest.raises(ValueError, match="Prepared output escapes workspace"):
        resolve_prepared_path(prepared, workspace)
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("redirect", ["leaf", "ancestor"])
def test_prepared_path_rejects_symlink_to_another_workspace_file(tmp_path: Path, redirect: str) -> None:
    parent = tmp_path / "accounts/acme/meetings"
    prepared = decode_prepared(json.dumps(_payload()))
    filename = Path(prepared.vault_relative_path).name
    if redirect == "leaf":
        parent.mkdir(parents=True)
        target = tmp_path / "TASKS.md"
        (parent / filename).symlink_to(target)
    else:
        other = tmp_path / "accounts/acme/pursuits"
        other.mkdir(parents=True)
        parent.symlink_to(other, target_is_directory=True)
        target = other / filename
    target.write_text("untouched", encoding="utf-8")
    with pytest.raises(ValueError, match="Prepared output redirects its authorized destination"):
        resolve_prepared_path(prepared, tmp_path)
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    ("field", "value"),
    [("cls", "future-class"), ("owner", 1), ("text", "x" * 8193), ("extra", "private-payload")],
)
def test_prepared_task_rejects_schema_changes(field: str, value: object) -> None:
    payload = _payload()
    task: dict[str, object] = {
        "text": "Send proposal",
        "cls": "my_task",
        "owner": "Alice",
        "rationale": "Assigned",
        "pursuit_label": "Acme",
    }
    task[field] = value
    payload["tasks"] = [task]
    with pytest.raises(ValueError, match=r"^Invalid prepared ingest output$"):
        decode_prepared(json.dumps(payload))
