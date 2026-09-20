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

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _display — display overrides and title-casing
# ---------------------------------------------------------------------------


def test_display_applies_override() -> None:
    """_display returns the override string for known slugs."""
    from fieldkit.commands.ingest.promote import _display

    assert _display("globalpay") == "GlobalPay"
    assert _display("sow") == "SOW"
    assert _display("rhoai") == "RHOAI"


def test_display_title_cases_unknown_slug() -> None:
    """_display title-cases slugs not in the override map."""
    from fieldkit.commands.ingest.promote import _display

    assert _display("acme-corp") == "Acme Corp"
    assert _display("my-account") == "My Account"


# ---------------------------------------------------------------------------
# _pursuit_label — with and without pursuits
# ---------------------------------------------------------------------------


def test_pursuit_label_with_pursuits() -> None:
    """_pursuit_label includes the first pursuit when pursuits list is non-empty."""
    from fieldkit.commands.ingest.promote import _pursuit_label

    result = _pursuit_label("acme-corp", ["ocp-migration"])
    assert "Acme Corp" in result
    assert "OCP" in result or "Ocp" in result or "Migration" in result


def test_pursuit_label_without_pursuits() -> None:
    """_pursuit_label returns just the account name when pursuits is empty."""
    from fieldkit.commands.ingest.promote import _pursuit_label

    result = _pursuit_label("acme-corp", [])
    assert "Acme Corp" in result
    assert "/" not in result


# ---------------------------------------------------------------------------
# _write_active — inserts under ## Active, creates section, skips duplicate
# ---------------------------------------------------------------------------


def test_write_active_inserts_under_existing_section(tmp_path: Path) -> None:
    """_write_active inserts the entry under an existing ## Active section."""
    from fieldkit.commands.ingest.promote import _write_active

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n## Waiting On\n", encoding="utf-8")

    _write_active(tasks, "Send the proposal", "Acme Corp")

    content = tasks.read_text(encoding="utf-8")
    assert "Send the proposal" in content
    assert "Acme Corp" in content


def test_write_active_creates_section_when_missing(tmp_path: Path) -> None:
    """_write_active creates ## Active section when it does not exist."""
    from fieldkit.commands.ingest.promote import _write_active

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("# My Tasks\n\nSome content.\n", encoding="utf-8")

    _write_active(tasks, "Book the demo", "Globalpay")

    content = tasks.read_text(encoding="utf-8")
    assert "## Active" in content
    assert "Book the demo" in content


def test_write_active_skips_duplicate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_write_active skips an item that is already in TASKS.md."""
    from fieldkit.commands.ingest.promote import _write_active

    item = "Send the proposal to legal for review"
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(f"## Active\n- {item}\n", encoding="utf-8")

    _write_active(tasks, item, "Acme Corp")

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

    _write_waiting(tasks, "Customer: Sign the SOW", "Acme Corp", "Sprint Review", "2026-05-01")

    content = tasks.read_text(encoding="utf-8")
    assert "Waiting on" in content
    assert "SOW" in content or "Sign" in content


def test_write_waiting_creates_section_when_missing(tmp_path: Path) -> None:
    """_write_waiting creates ## Waiting On section when it does not exist."""
    from fieldkit.commands.ingest.promote import _write_waiting

    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n\n", encoding="utf-8")

    _write_waiting(tasks, "Alice: Approve the budget", "Acme Corp", "Budget Review", "2026-06-01")

    content = tasks.read_text(encoding="utf-8")
    assert "## Waiting On" in content
    assert "Waiting on" in content


def test_write_waiting_skips_duplicate(tmp_path: Path) -> None:
    """_write_waiting skips an item already in TASKS.md."""
    from fieldkit.commands.ingest.promote import _write_waiting

    item = "Customer: Sign the contract before end of month"
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(f"## Waiting On\n- Waiting on re: {item[:60]}\n", encoding="utf-8")

    _write_waiting(tasks, item, "Acme Corp", "Deal Review", "2026-05-15")

    content = tasks.read_text(encoding="utf-8")
    # The item[:60] check in _write_waiting should prevent duplicate
    assert content.count(item[:60]) == 1


# ---------------------------------------------------------------------------
# _promote_file — no frontmatter, YAML error, waiting-on choice, skip choice
# ---------------------------------------------------------------------------


def test_promote_file_returns_false_on_no_frontmatter(tmp_path: Path) -> None:
    """_promote_file returns False when meeting note has no YAML frontmatter."""
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-no-fm.md"
    meeting.write_text("# Notes\n\nNo frontmatter here.\n", encoding="utf-8")
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n", encoding="utf-8")

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False, "_promote_file must return False when no frontmatter"


def test_promote_file_returns_false_on_yaml_error(tmp_path: Path) -> None:
    """_promote_file returns False when frontmatter has invalid YAML."""
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-05-01-bad-yaml.md"
    meeting.write_text(
        "---\nkey: [unclosed bracket\n---\n# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("## Active\n", encoding="utf-8")

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False, "_promote_file must return False on YAML error"


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

    def mock_write_active(tasks_path: Path, item: str, label: str) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "m")
    monkeypatch.setattr(promote_mod, "_write_active", mock_write_active)

    result = _promote_file(meeting, tasks, "Test User", "test@example.com")  # pii-guard: ignore

    assert result is False
    assert call_count == 2, f"Expected 2 _write_active calls, got {call_count}"
