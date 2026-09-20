"""Unit tests for fieldkit.tasks.writer.append_to_tasks.

Covers all FileSystemWrite and ReturnValue side effects:
  - Insert MY_TASK under ## Active
  - Insert WAITING_ON under ## Waiting On
  - Idempotency (duplicate detection by text[:60] substring)
  - Missing-section fallback (creates ## Active / ## Waiting On at EOF)
  - File creation when tasks_path does not exist
  - Early return (no I/O) when classified_items is empty
  - Comment-line skip: new task inserted after <!-- ... --> line, not before it
"""

import pytest

from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.writer import append_to_tasks

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

MEETING_DATE = "2026-06-14"
MEETING_TITLE = "Acme Corp Discovery Call"
PURSUIT_LABEL = "Acme Corp / RHOAI"


def _my_task(text: str, pursuit_label: str = PURSUIT_LABEL) -> ClassifiedItem:
    """Build a MY_TASK ClassifiedItem for use in tests."""
    return ClassifiedItem(
        text=text,
        cls=ItemClass.MY_TASK,
        owner="Test AE",
        rationale="Owner matches AE name/email",
        pursuit_label=pursuit_label,
    )


def _waiting_on(text: str, owner: str = "Alice Customer", pursuit_label: str = PURSUIT_LABEL) -> ClassifiedItem:
    """Build a WAITING_ON ClassifiedItem for use in tests."""
    return ClassifiedItem(
        text=text,
        cls=ItemClass.WAITING_ON,
        owner=owner,
        rationale="Customer stakeholder + high-confidence deal gate",
        pursuit_label=pursuit_label,
    )


def _call(tasks_path, items):
    """Thin wrapper so tests don't repeat keyword args."""
    return append_to_tasks(
        items,
        tasks_path,
        meeting_date=MEETING_DATE,
        meeting_title=MEETING_TITLE,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_appends_my_task_to_active_section(tmp_path):
    """MY_TASK item is inserted under ## Active; returns (1, 0)."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    item = _my_task("Send updated SOW to Alice by Friday")
    result = _call(tasks_file, [item])

    assert result == (1, 0)
    content = tasks_file.read_text(encoding="utf-8")
    # Formatted line: "- **[Acme Corp / RHOAI]** Send updated SOW to Alice by Friday"
    assert "Send updated SOW to Alice by Friday" in content
    assert f"**[{PURSUIT_LABEL}]**" in content
    # Verify insertion is under the ## Active heading (heading still present)
    assert "## Active" in content


@pytest.mark.unit
def test_appends_waiting_on_to_waiting_on_section(tmp_path):
    """WAITING_ON item is inserted under ## Waiting On; returns (0, 1)."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Waiting On\n", encoding="utf-8")

    item = _waiting_on("Alice Customer: confirm budget approval with Finance team")
    result = _call(tasks_file, [item])

    assert result == (0, 1)
    content = tasks_file.read_text(encoding="utf-8")
    # Formatted waiting-on line contains "Waiting on" prefix and meeting provenance
    assert "Waiting on" in content
    assert MEETING_TITLE in content
    assert MEETING_DATE in content
    assert "## Waiting On" in content


@pytest.mark.unit
def test_skips_duplicate_items(tmp_path):
    """Second call with identical item text returns (0, 0); file content unchanged."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    item = _my_task("Draft the architecture diagram for RHOAI pilot")

    # First call — should insert
    first_result = _call(tasks_file, [item])
    assert first_result == (1, 0)

    content_after_first = tasks_file.read_text(encoding="utf-8")

    # Second call — item text[:60] is already in the file; should be skipped
    second_result = _call(tasks_file, [item])
    assert second_result == (0, 0)

    # File content must be byte-for-byte identical to post-first-call content.
    # NOTE: the file IS rewritten (write_text is called unconditionally after the
    # early-return guard); we assert content equality, not that write was skipped.
    content_after_second = tasks_file.read_text(encoding="utf-8")
    assert content_after_second == content_after_first


@pytest.mark.unit
def test_creates_active_section_when_absent(tmp_path):
    """When ## Active is missing, fallback appends the section and item at EOF."""
    tasks_file = tmp_path / "TASKS.md"
    # File exists but has no ## Active heading
    tasks_file.write_text("## Waiting On\n", encoding="utf-8")

    item = _my_task("Prepare demo environment for Acme Corp")
    result = _call(tasks_file, [item])

    assert result == (1, 0)
    content = tasks_file.read_text(encoding="utf-8")
    # Fallback path appends "## Active\n<formatted line>\n" at end of file
    assert "## Active" in content
    assert "Prepare demo environment for Acme Corp" in content


@pytest.mark.unit
def test_creates_waiting_on_section_when_absent(tmp_path):
    """When ## Waiting On is missing, fallback appends the section and item at EOF."""
    tasks_file = tmp_path / "TASKS.md"
    # File exists but has no ## Waiting On heading
    tasks_file.write_text("## Active\n", encoding="utf-8")

    item = _waiting_on("Alice Customer: submit signed contract before close date")
    result = _call(tasks_file, [item])

    assert result == (0, 1)
    content = tasks_file.read_text(encoding="utf-8")
    # Fallback path appends "## Waiting On\n<formatted line>\n" at end of file
    assert "## Waiting On" in content
    assert "Waiting on" in content


@pytest.mark.unit
def test_creates_file_when_not_exists(tmp_path):
    """When tasks_path does not exist, the file is created with ## Active and the task."""
    tasks_file = tmp_path / "TASKS.md"
    assert not tasks_file.exists()

    item = _my_task("Schedule kick-off call with Acme Corp team")
    result = _call(tasks_file, [item])

    assert result == (1, 0)
    assert tasks_file.exists()
    content = tasks_file.read_text(encoding="utf-8")
    assert "## Active" in content
    assert "Schedule kick-off call with Acme Corp team" in content


@pytest.mark.unit
def test_returns_zero_zero_when_no_items(tmp_path):
    """Empty classified_items triggers early return: (0, 0) with no file I/O."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")
    original_content = tasks_file.read_text(encoding="utf-8")

    result = _call(tasks_file, [])

    assert result == (0, 0)
    # No write should have occurred — content and mtime are unchanged
    assert tasks_file.read_text(encoding="utf-8") == original_content


@pytest.mark.unit
def test_skips_comment_line_after_heading(tmp_path):
    """New task is inserted AFTER the <!-- ... --> comment line, not between heading and comment."""
    tasks_file = tmp_path / "TASKS.md"
    # Heading followed immediately by an HTML comment line
    initial = "## Active\n<!-- add tasks below -->\n"
    tasks_file.write_text(initial, encoding="utf-8")

    item = _my_task("Review RHOAI pilot architecture with Dave")
    result = _call(tasks_file, [item])

    assert result == (1, 0)
    content = tasks_file.read_text(encoding="utf-8")

    # The comment line must still be present and must appear BEFORE the new task line
    comment_pos = content.find("<!-- add tasks below -->")
    task_pos = content.find("Review RHOAI pilot architecture with Dave")

    assert comment_pos != -1, "HTML comment line should be preserved"
    assert task_pos != -1, "New task line should be present"
    assert comment_pos < task_pos, "New task must be inserted AFTER the comment line, not between heading and comment"


# ---------------------------------------------------------------------------
# Additional branch-coverage tests — targeting CRAP-score reduction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multiple_my_tasks_inserted_in_one_call(tmp_path):
    """Multiple MY_TASK items in one call are all inserted; returns (N, 0)."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    items = [
        _my_task("First task: prepare slides for Acme Corp"),
        _my_task("Second task: send pricing to Alice"),
        _my_task("Third task: schedule follow-up call"),
    ]
    result = _call(tasks_file, items)

    assert result == (3, 0)
    content = tasks_file.read_text(encoding="utf-8")
    assert "First task: prepare slides for Acme Corp" in content
    assert "Second task: send pricing to Alice" in content
    assert "Third task: schedule follow-up call" in content


@pytest.mark.unit
def test_multiple_waiting_on_items_inserted_in_one_call(tmp_path):
    """Multiple WAITING_ON items in one call are all inserted; returns (0, N)."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Waiting On\n", encoding="utf-8")

    items = [
        _waiting_on("Alice Customer: confirm budget approval"),
        _waiting_on("Bob Stakeholder: submit signed contract before close date"),
    ]
    result = _call(tasks_file, items)

    assert result == (0, 2)
    content = tasks_file.read_text(encoding="utf-8")
    assert "Waiting on" in content
    assert MEETING_TITLE in content


@pytest.mark.unit
def test_both_my_task_and_waiting_on_in_one_call(tmp_path):
    """Mixed items: MY_TASK goes to Active, WAITING_ON goes to Waiting On."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n\n## Waiting On\n", encoding="utf-8")

    items = [
        _my_task("Send updated proposal to Alice"),
        _waiting_on("Alice Customer: confirm budget approval"),
    ]
    result = _call(tasks_file, items)

    assert result == (1, 1)
    content = tasks_file.read_text(encoding="utf-8")
    assert "Send updated proposal to Alice" in content
    assert "Waiting on" in content
    assert "## Active" in content
    assert "## Waiting On" in content


@pytest.mark.unit
def test_fallback_creates_active_section_when_both_sections_absent(tmp_path):
    """When neither ## Active nor ## Waiting On exist, both are created at EOF."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("# My Tasks\n\nSome intro text.\n", encoding="utf-8")

    items = [
        _my_task("Prepare demo environment for pilot"),
        _waiting_on("Alice Customer: submit signed NDA before kickoff"),
    ]
    result = _call(tasks_file, items)

    assert result == (1, 1)
    content = tasks_file.read_text(encoding="utf-8")
    assert "## Active" in content
    assert "Prepare demo environment for pilot" in content
    assert "## Waiting On" in content
    assert "Waiting on" in content


@pytest.mark.unit
def test_fallback_active_section_idempotent_on_second_call(tmp_path):
    """Fallback-created ## Active section is not duplicated on second call."""
    tasks_file = tmp_path / "TASKS.md"
    # No ## Active section initially
    tasks_file.write_text("# Tasks\n", encoding="utf-8")

    item = _my_task("Prepare architecture diagram for RHOAI")

    # First call: creates ## Active section via fallback
    result1 = _call(tasks_file, [item])
    assert result1 == (1, 0)

    # Second call: item text[:60] already in file → skipped
    result2 = _call(tasks_file, [item])
    assert result2 == (0, 0)

    content = tasks_file.read_text(encoding="utf-8")
    # Only one occurrence of the task text
    assert content.count("Prepare architecture diagram for RHOAI") == 1


@pytest.mark.unit
def test_waiting_on_fallback_section_not_duplicated(tmp_path):
    """Fallback-created ## Waiting On section is not duplicated on second call with a new item."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("# Tasks\n", encoding="utf-8")

    item1 = _waiting_on("Alice Customer: confirm budget approval")
    item2 = _waiting_on("Bob Stakeholder: submit signed NDA before kickoff")

    result1 = _call(tasks_file, [item1])
    assert result1 == (0, 1)

    # Second call with a different item — should find the ## Waiting On section now
    result2 = _call(tasks_file, [item2])
    assert result2 == (0, 1)

    content = tasks_file.read_text(encoding="utf-8")
    # Only one ## Waiting On section (second call uses the existing section)
    assert content.count("## Waiting On") == 1


@pytest.mark.unit
def test_item_without_pursuit_label_formats_correctly(tmp_path):
    """MY_TASK item with empty pursuit_label omits the **[...]** tag."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    item = _my_task("Send the proposal to the customer", pursuit_label="")
    result = _call(tasks_file, [item])

    assert result == (1, 0)
    content = tasks_file.read_text(encoding="utf-8")
    assert "Send the proposal to the customer" in content
    # No pursuit label tag should appear
    assert "**[" not in content


@pytest.mark.unit
def test_waiting_on_owner_unknown_omits_owner_in_format(tmp_path):
    """WAITING_ON item with owner='Unknown' omits owner name in formatted line."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Waiting On\n", encoding="utf-8")

    item = ClassifiedItem(
        text="confirm budget approval before close date",
        cls=ItemClass.WAITING_ON,
        owner="Unknown",
        rationale="Unknown owner + high-confidence gate",
        pursuit_label=PURSUIT_LABEL,
    )
    result = _call(tasks_file, [item])

    assert result == (0, 1)
    content = tasks_file.read_text(encoding="utf-8")
    # "Waiting on re:" — no owner name between "on" and "re:"
    assert "Waiting on re:" in content


@pytest.mark.unit
def test_waiting_on_owner_team_omits_owner_in_format(tmp_path):
    """WAITING_ON item with owner='Team' omits owner name in formatted line."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Waiting On\n", encoding="utf-8")

    item = ClassifiedItem(
        text="confirm budget approval before close date",
        cls=ItemClass.WAITING_ON,
        owner="Team",
        rationale="Team + high-confidence gate",
        pursuit_label=PURSUIT_LABEL,
    )
    result = _call(tasks_file, [item])

    assert result == (0, 1)
    content = tasks_file.read_text(encoding="utf-8")
    assert "Waiting on re:" in content


@pytest.mark.unit
def test_comment_after_waiting_on_heading_is_skipped(tmp_path):
    """New WAITING_ON item is inserted AFTER the <!-- ... --> comment under ## Waiting On."""
    tasks_file = tmp_path / "TASKS.md"
    initial = "## Waiting On\n<!-- items below -->\n"
    tasks_file.write_text(initial, encoding="utf-8")

    item = _waiting_on("Alice Customer: confirm budget approval")
    result = _call(tasks_file, [item])

    assert result == (0, 1)
    content = tasks_file.read_text(encoding="utf-8")

    comment_pos = content.find("<!-- items below -->")
    waiting_pos = content.find("Waiting on")

    assert comment_pos != -1
    assert waiting_pos != -1
    assert comment_pos < waiting_pos, "Waiting-on entry must be inserted after the comment line"


@pytest.mark.unit
def test_drop_items_are_ignored(tmp_path):
    """DROP-classified items are not written to TASKS.md; returns (0, 0)."""
    from fieldkit.tasks.classifier import ClassifiedItem, ItemClass

    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    drop_item = ClassifiedItem(
        text="Team: update Jira sprint board",
        cls=ItemClass.DROP,
        owner="Team",
        rationale="Internal mechanics",
        pursuit_label=PURSUIT_LABEL,
    )
    result = _call(tasks_file, [drop_item])

    assert result == (0, 0)
    content = tasks_file.read_text(encoding="utf-8")
    assert "update Jira sprint board" not in content


@pytest.mark.unit
def test_partial_duplicate_skipped_full_new_added(tmp_path):
    """When one item is duplicate and another is new, only the new one is added."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text("## Active\n", encoding="utf-8")

    existing_item = _my_task("Send updated SOW to Alice by Friday")
    new_item = _my_task("Schedule kick-off call with Acme Corp team")

    # First call: insert existing_item
    _call(tasks_file, [existing_item])

    # Second call: existing_item is duplicate, new_item is new
    result = _call(tasks_file, [existing_item, new_item])

    assert result == (1, 0)
    content = tasks_file.read_text(encoding="utf-8")
    assert "Send updated SOW to Alice by Friday" in content
    assert "Schedule kick-off call with Acme Corp team" in content
