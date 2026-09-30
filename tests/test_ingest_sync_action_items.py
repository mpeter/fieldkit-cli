"""In-memory task classification, failure propagation, and attendee handling."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError
from fieldkit.ingest import writeback
from fieldkit.ingest.writeback import _attendee_names_from_note
from fieldkit.tasks.classifier import ItemClass

pytestmark = pytest.mark.unit


def test_classification_uses_selected_workspace_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = tmp_path / "selected"
    configured = tmp_path / "configured"
    for root, name in [(selected, "Carol"), (configured, "Dave")]:
        (root / "config").mkdir(parents=True)
        (root / "config/accounts.yaml").write_text(
            f"accounts:\n  acme:\n    internal_team_display_names: [{name}]\n", encoding="utf-8"
        )
    personal_config = tmp_path / "config.yaml"
    personal_config.write_text(f"fieldkit_home: {configured}\n", encoding="utf-8")
    monkeypatch.setattr("fieldkit.config._loader.CONFIG_PATH", personal_config)
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[]) as classify,
    ):
        result = writeback.classify_meeting_tasks(
            data_root=selected,
            action_items=["Send proposal"],
            pursuits=[],
            account="acme",
            note_content="---\nattendees_internal: []\nattendees_external: []\n---\n",
        )
    assert result == ()
    assert classify.call_args.kwargs["internal_team_names"] == ["Carol"]


def test_no_action_items_require_no_task_dependencies(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.get_user_name") as user,
        patch("fieldkit.config.get_user_email") as email,
        patch("fieldkit.config.get_accounts_config") as accounts,
        patch("fieldkit.ingest.writeback._attendee_names_from_note") as attendees,
        patch("fieldkit.tasks.classifier.classify_action_items") as classifier,
    ):
        result = writeback.classify_meeting_tasks(
            data_root=tmp_path, action_items=[], pursuits=[], account="acme", note_content="Not valid frontmatter"
        )
    assert result == ()
    user.assert_not_called()
    email.assert_not_called()
    accounts.assert_not_called()
    attendees.assert_not_called()
    classifier.assert_not_called()


@pytest.mark.parametrize(
    "account,pursuits,label",
    [
        ("acme", [], "Acme"),
        ("acme-bank", [], "Acme Bank"),
        ("acme-corp", ["ocp-migration"], "Acme-Corp / OCP Migration"),
        ("acme", ["aap-upgrade"], "Acme / AAP Upgrade"),
        ("acme", ["rhoai-pilot"], "Acme / RHOAI Pilot"),
    ],
)
def test_classification_preserves_inputs_and_ownership_context(
    tmp_path: Path, account: str, pursuits: list[str], label: str
) -> None:
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),
        patch(
            "fieldkit.config.get_accounts_config",
            return_value={"accounts": {account: {"internal_team_display_names": ["Carol"]}}},
        ) as config,
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[]) as classify,
    ):
        result = writeback.classify_meeting_tasks(
            data_root=tmp_path,
            action_items=["Send proposal"],
            pursuits=pursuits,
            account=account,
            note_content="---\nattendees_internal: [Alice, Carol]\nattendees_external: [Bob, Carol]\n---\n",
        )
    assert result == ()
    config.assert_called_once_with(workspace_root=tmp_path, strict=True)
    classify.assert_called_once_with(
        ["Send proposal"],
        user_name="Alice",
        user_email="alice@example.com",
        stakeholder_names=["Bob"],
        internal_team_names=["Carol", "Alice"],
        pursuit_label=label,
    )


@pytest.mark.parametrize("target", ["fieldkit.config.get_user_name", "fieldkit.tasks.classifier.classify_action_items"])
def test_classification_dependency_failure_propagates(tmp_path: Path, target: str) -> None:
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value=""),
        patch("fieldkit.config.get_accounts_config", return_value={}),
        patch(target, side_effect=RuntimeError("classification unavailable")),
        pytest.raises(RuntimeError, match="classification unavailable"),
    ):
        writeback.classify_meeting_tasks(
            data_root=tmp_path,
            action_items=["Send proposal"],
            pursuits=[],
            account="acme",
            note_content="---\nattendees_internal: []\n---\n",
        )


@pytest.mark.parametrize("contents", ["accounts: [", "- invalid root"])
def test_classification_stops_before_classifier_on_invalid_accounts_file(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "config/accounts.yaml"
    path.parent.mkdir()
    path.write_text(contents, encoding="utf-8")
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),
        patch("fieldkit.tasks.classifier.classify_action_items") as classify,
        pytest.raises(ConfigError, match=r"Invalid or unreadable accounts\.yaml"),
    ):
        writeback.classify_meeting_tasks(
            data_root=tmp_path,
            action_items=["Alice: send proposal"],
            pursuits=[],
            account="acme",
            note_content="---\nattendees_internal: []\n---\nMeeting\n",
        )
    classify.assert_not_called()


def test_classification_accepts_unwritten_note_without_file_effects(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch.object(Path, "read_text", side_effect=AssertionError("Unexpected file read")),
        patch("fieldkit.tasks.writer.append_to_tasks") as append,
    ):
        result = writeback.classify_meeting_tasks(
            data_root=tmp_path,
            action_items=["Alice: send proposal"],
            pursuits=[],
            account="acme",
            note_content="---\nattendees_internal: [Alice]\nattendees_external: [Bob]\n---\nMeeting\n",
        )
    assert len(result) == 1
    assert result[0].cls == ItemClass.MY_TASK
    append.assert_not_called()


@pytest.mark.parametrize(
    "accounts",
    [
        {"accounts": None},
        {"accounts": []},
        {"accounts": {"acme": "invalid"}},
        {"accounts": {"acme": {"internal_team_display_names": "Alice"}}},
        {"accounts": {"acme": {"internal_team_display_names": [1]}}},
        {"accounts": {"acme": {"internal_team_display_names": [""]}}},
        {"accounts": {"acme": {"internal_team_display_names": ["  "]}}},
    ],
)
def test_classification_rejects_malformed_account_ownership(tmp_path: Path, accounts: dict[str, object]) -> None:
    with (
        patch("fieldkit.config.get_user_name", return_value="Alice"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),
        patch("fieldkit.config.get_accounts_config", return_value=accounts),
        patch("fieldkit.tasks.classifier.classify_action_items") as classify,
        pytest.raises(ValueError, match=r"account task configuration|internal_team_display_names"),
    ):
        writeback.classify_meeting_tasks(
            data_root=tmp_path,
            action_items=["Alice: send proposal"],
            pursuits=[],
            account="acme",
            note_content="---\nattendees_internal: []\n---\nMeeting\n",
        )
    classify.assert_not_called()


@pytest.mark.parametrize(
    "frontmatter",
    [
        "- Alice",
        "attendees_internal: Alice",
        "attendees_external: 3",
        "attendees_internal: [3]",
        'attendees_internal: [""]',
        'attendees_external: ["  "]',
        'attendees_external: ["(Example Corp)"]',
    ],
)
def test_invalid_attendee_metadata_is_rejected(frontmatter: str) -> None:
    with pytest.raises(ValueError, match=r"frontmatter|attendees"):
        _attendee_names_from_note(f"---\n{frontmatter}\n---\n# Meeting\n", [])


def test_attendee_names_normalize_surrounding_whitespace() -> None:
    result = _attendee_names_from_note(
        '---\nattendees_internal: [" Alice "]\nattendees_external: [" Alice ", " Bob (Acme) "]\n---\n', []
    )
    assert result == (["Alice"], ["Bob"])
