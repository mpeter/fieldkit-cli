"""Extended unit tests for fieldkit/ingest/promote.py.

Targets the 68 uncovered lines (59% → target ≥ 75%) by exercising:
  - _display: display overrides and title-casing
  - _pursuit_label: with and without pursuits
  - _write_active: inserts under ## Active, creates section if missing, skips duplicate
  - _write_waiting: inserts under ## Waiting On, creates section if missing, skips duplicate
  - _promote_file: no frontmatter, YAML error, waiting-on choice, skip choice
"""

from pathlib import Path

import pytest

from fieldkit.ingest.prepared import ReplayIntent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "metadata", ["pursuits: abc", "pursuits: [3]", "account: [acme]", "meeting_title: 4", "meeting_date: [today]"]
)
def test_promote_rejects_coerced_metadata_before_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    from fieldkit.commands.ingest import promote

    note = tmp_path / "meeting.md"
    note.write_text(f"---\nsource_id: source-1\naction_items: [Send proposal]\n{metadata}\n---\n", encoding="utf-8")
    monkeypatch.setattr(promote, "_prompt_item", lambda *args: pytest.fail("Invalid metadata must not prompt"))
    with pytest.raises(ValueError, match="Invalid meeting"):
        promote._promote_file(note, tmp_path / "TASKS.md", "Alice", "alice@example.com")
    assert not (tmp_path / "TASKS.md").exists()


@pytest.mark.parametrize("account", ["acme", "acme-corp", "globalpay"])
def test_auto_task_then_promotion_skips_mine_and_continues_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], account: str
) -> None:
    import hashlib

    from fieldkit.commands.ingest import promote
    from fieldkit.ingest.paths import compute_vault_path
    from fieldkit.ingest.prepared import PreparedMeeting, PreparedTask
    from fieldkit.ingest.task_effect import publish_prepared_tasks
    from fieldkit.ingest.writeback import classify_meeting_tasks

    note = tmp_path / "meeting.md"
    note.write_text(
        f"---\nsource_id: source-1\naccount: {account}\nmeeting_title: Review\n"
        "meeting_date: '2026-09-27'\nattendees_internal: [Alice]\nattendees_external: [Bob]\n"
        "action_items:\n  - 'Alice: Send proposal'\n  - 'Bob: Approve budget'\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("fieldkit.config.get_user_name", lambda: "Alice")
    monkeypatch.setattr("fieldkit.config.get_user_email", lambda: "alice@example.com")
    monkeypatch.setattr("fieldkit.config.get_accounts_config", lambda *, strict, workspace_root: {})
    classified = classify_meeting_tasks(
        data_root=tmp_path,
        action_items=["Alice: Send proposal", "Bob: Approve budget"],
        pursuits=[],
        account=account,
        note_content=note.read_text(encoding="utf-8"),
    )
    content = note.read_text(encoding="utf-8")
    prepared = PreparedMeeting(
        schema_version=1,
        pipeline_id="transcript-ingest",
        source_id="source-1",
        pipeline_version="0.1.0",
        vault_relative_path=compute_vault_path(
            Path(), account, "2026-09-27", "Review", source_id="source-1"
        ).as_posix(),
        note_content=content,
        note_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        account=account,
        meeting_date="2026-09-27",
        meeting_title="Review",
        pursuits=(),
        action_items=("Alice: Send proposal", "Bob: Approve budget"),
        tasks=tuple(
            PreparedTask(
                text=item.text,
                cls=item.cls.value,
                owner=item.owner,
                rationale=item.rationale,
                pursuit_label=item.pursuit_label,
            )
            for item in classified
        ),
        degraded=False,
    )
    assert publish_prepared_tasks(ReplayIntent(prepared), tmp_path) == (1, 0)
    choices = iter(["m", "w"])
    monkeypatch.setattr(promote, "_prompt_item", lambda *args: next(choices))
    result = promote._promote_file(note, tmp_path / "TASKS.md", "Alice", "alice@example.com")
    assert result is False
    assert "Done: 0 active, 1 waiting-on, 1 skipped" in capsys.readouterr().out
    content = (tmp_path / "TASKS.md").read_text(encoding="utf-8")
    assert content.count("Send proposal") == 1
    assert content.count("Approve budget") == 1


def test_promote_duplicate_does_not_report_an_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest import promote

    note = tmp_path / "meeting.md"
    note.write_text("---\nsource_id: source-1\naction_items: [Send proposal]\n---\n", encoding="utf-8")
    tasks = tmp_path / "TASKS.md"
    assert promote._write_active(tasks, "Send proposal", "Unknown", source_id="source-1", position=0)
    monkeypatch.setattr(promote, "_prompt_item", lambda *args: "m")
    result = promote._promote_file(note, tasks, "Alice", "alice@example.com")
    assert result is False
    output = capsys.readouterr().out
    assert "Done: 0 active, 0 waiting-on, 1 skipped" in output
    assert "added to Active" not in output


# ---------------------------------------------------------------------------
# _display — display overrides and title-casing
# ---------------------------------------------------------------------------


def test_display_applies_override() -> None:
    """_display returns the override string for known slugs."""
    from fieldkit.ingest.writeback import _display_slug

    assert _display_slug("globalpay") == "GlobalPay"
    assert _display_slug("sow") == "SOW"
    assert _display_slug("rhoai") == "RHOAI"


def test_display_title_cases_unknown_slug() -> None:
    """_display title-cases slugs not in the override map."""
    from fieldkit.ingest.writeback import _display_slug

    assert _display_slug("acme-corp") == "Acme Corp"
    assert _display_slug("my-account") == "My Account"


# ---------------------------------------------------------------------------
# _pursuit_label — with and without pursuits
# ---------------------------------------------------------------------------


def test_pursuit_label_with_pursuits() -> None:
    """_pursuit_label includes the first pursuit when pursuits list is non-empty."""
    from fieldkit.ingest.writeback import build_pursuit_label

    result = build_pursuit_label("acme-corp", ["ocp-migration"])
    assert "Acme-Corp" in result
    assert "OCP" in result or "Ocp" in result or "Migration" in result


def test_pursuit_label_without_pursuits() -> None:
    """_pursuit_label returns just the account name when pursuits is empty."""
    from fieldkit.ingest.writeback import build_pursuit_label

    result = build_pursuit_label("acme-corp", [])
    assert "Acme-Corp" in result
    assert "/" not in result


# ---------------------------------------------------------------------------
# _write_active — inserts under ## Active, creates section, skips duplicate
# ---------------------------------------------------------------------------


def test_write_active_inserts_under_existing_section(tmp_path: Path) -> None:
    """_write_active inserts the entry under an existing ## Active section."""
    from fieldkit.commands.ingest.promote import _write_active

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n## Waiting On\n", encoding="utf-8")

    _write_active(tasks, "Send the proposal", "Acme Corp", source_id="source-1", position=0)

    content = tasks.read_text(encoding="utf-8")
    assert "Send the proposal" in content
    assert "Acme Corp" in content


def test_write_active_creates_section_when_missing(tmp_path: Path) -> None:
    """_write_active creates ## Active section when it does not exist."""
    from fieldkit.commands.ingest.promote import _write_active

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("# My Tasks\n\nSome content.\n", encoding="utf-8")

    _write_active(tasks, "Book the demo", "Globalpay", source_id="source-1", position=0)

    content = tasks.read_text(encoding="utf-8")
    assert "## Active" in content
    assert "Book the demo" in content


def test_write_active_skips_duplicate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_write_active skips an item that is already in TASKS.md."""
    from fieldkit.commands.ingest.promote import _write_active

    item = "Send the proposal to legal for review"
    tasks = tmp_path / "TASKS.md"
    assert _write_active(tasks, item, "Acme Corp", source_id="source-1", position=0)

    _write_active(tasks, item, "Acme Corp", source_id="source-1", position=0)

    content = tasks.read_text(encoding="utf-8")
    # Item should appear exactly once
    assert content.count(item[:60]) == 1


# ---------------------------------------------------------------------------
# _write_waiting — inserts under ## Waiting On, creates section, skips duplicate
# ---------------------------------------------------------------------------


def test_write_waiting_inserts_under_existing_section(tmp_path: Path) -> None:
    """_write_waiting inserts the entry under an existing ## Waiting On section."""
    from fieldkit.commands.ingest.promote import _write_waiting

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n## Waiting On\n\n", encoding="utf-8")

    _write_waiting(
        tasks,
        "Customer: Sign the SOW",
        "Acme Corp",
        meeting_title="Sprint Review",
        meeting_date="2026-05-01",
        source_id="source-1",
        position=0,
    )

    content = tasks.read_text(encoding="utf-8")
    assert "Waiting on" in content
    assert "SOW" in content or "Sign" in content


def test_write_waiting_creates_section_when_missing(tmp_path: Path) -> None:
    """_write_waiting creates ## Waiting On section when it does not exist."""
    from fieldkit.commands.ingest.promote import _write_waiting

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n", encoding="utf-8")

    _write_waiting(
        tasks,
        "Alice: Approve the budget",
        "Acme Corp",
        meeting_title="Budget Review",
        meeting_date="2026-06-01",
        source_id="source-1",
        position=0,
    )

    content = tasks.read_text(encoding="utf-8")
    assert "## Waiting On" in content
    assert "Waiting on" in content


def test_write_waiting_skips_duplicate(tmp_path: Path) -> None:
    """_write_waiting skips an item already in TASKS.md."""
    from fieldkit.commands.ingest.promote import _write_waiting

    item = "Customer: Sign the contract before end of month"
    tasks = tmp_path / "TASKS.md"
    assert _write_waiting(
        tasks,
        item,
        "Acme Corp",
        meeting_title="Deal Review",
        meeting_date="2026-05-15",
        source_id="source-1",
        position=0,
    )

    _write_waiting(
        tasks,
        item,
        "Acme Corp",
        meeting_title="Deal Review",
        meeting_date="2026-05-15",
        source_id="source-1",
        position=0,
    )

    content = tasks.read_text(encoding="utf-8")
    assert content.count("Sign the contract before end of month") == 1


# ---------------------------------------------------------------------------
# _promote_file — no frontmatter, YAML error, waiting-on choice, skip choice
# ---------------------------------------------------------------------------


def test_promote_file_rejects_missing_frontmatter(tmp_path: Path) -> None:
    """Missing metadata must not look like successful promotion."""
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-no-fm.md"
    meeting.write_text("# Notes\n\nNo frontmatter here.\n", encoding="utf-8")
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid meeting frontmatter"):
        _promote_file(meeting, tasks, "Test User", "user@example.com")


def test_promote_file_rejects_yaml_error(tmp_path: Path) -> None:
    """Invalid YAML must not look like successful promotion."""
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-bad-yaml.md"
    meeting.write_text(
        "---\nkey: [unclosed bracket\n---\n# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid meeting frontmatter"):
        _promote_file(meeting, tasks, "Test User", "user@example.com")


def test_promote_file_waiting_on_choice_writes_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_promote_file with 'w' choice writes item to Waiting On section."""
    from fieldkit.commands.ingest import promote as promote_mod
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-meeting.md"
    meeting.write_text(
        "---\n"
        "meeting_title: Deal Review\n"
        "meeting_date: '2026-05-01'\n"
        "account: acme\n"
        "pursuits: []\n"
        "source_id: source-1\n"
        "action_items:\n"
        "  - Customer needs to sign the docusign envelope\n"
        "---\n"
        "# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n## Waiting On\n", encoding="utf-8")

    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "w")

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False, "_promote_file must return False after normal completion"
    content = tasks.read_text(encoding="utf-8")
    assert "Waiting on" in content or "docusign" in content.lower()


def test_promote_file_skip_choice_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_promote_file with 's' choice skips item without writing to TASKS.md."""
    from fieldkit.commands.ingest import promote as promote_mod
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-meeting.md"
    meeting.write_text(
        "---\n"
        "meeting_title: Team Sync\n"
        "meeting_date: '2026-05-01'\n"
        "account: acme\n"
        "pursuits: []\n"
        "source_id: source-1\n"
        "action_items:\n"
        "  - Team discussed the roadmap priorities\n"
        "---\n"
        "# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"
    initial_content = "## Active\n"
    tasks.write_text(initial_content, encoding="utf-8")

    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "s")

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False
    # Content should not have changed significantly (no new items added)
    content = tasks.read_text(encoding="utf-8")
    assert "roadmap" not in content, "Skipped item must not be written to TASKS.md"


def test_promote_file_multiple_items_all_mine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_promote_file with multiple items all chosen as 'm' writes all to Active."""
    from fieldkit.commands.ingest import promote as promote_mod
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-meeting.md"
    meeting.write_text(
        "---\n"
        "meeting_title: Planning\n"
        "meeting_date: '2026-05-01'\n"
        "account: acme\n"
        "pursuits: []\n"
        "source_id: source-1\n"
        "action_items:\n"
        "  - Send the proposal\n"
        "  - Schedule the follow-up call\n"
        "---\n"
        "# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n", encoding="utf-8")

    call_count = 0

    def mock_write_active(tasks_path: Path, item: str, label: str, *, source_id: str, position: int) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "m")
    monkeypatch.setattr(promote_mod, "_write_active", mock_write_active)

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False
    assert call_count == 2, f"Expected 2 _write_active calls, got {call_count}"
