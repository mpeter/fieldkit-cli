"""Tests for implementation change: promote handles stdin EOF as a clean quit (exit 0).

Spec: openspec/changes/ingest-correctness/specs/ingest-ux-correctness/spec.md
  - Scenario: EOF during item prompt exits cleanly
  - Scenario: Ctrl-C (SIGINT) during prompt exits cleanly
  - Scenario: Interactive confirmation — happy path unchanged
"""

import click
import pytest

pytestmark = pytest.mark.unit


# ── TestPromptItemAbort (flattened) ─────────────────────────────────────────


def test_prompt_item_abort_abort_returns_q(monkeypatch: pytest.MonkeyPatch) -> None:
    """click.exceptions.Abort from click.prompt must be caught and return 'q'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(
        click,
        "prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(click.exceptions.Abort()),
    )

    result = _prompt_item(1, 3, "Send the proposal by Friday", "m")
    assert result == "q"


def test_prompt_item_abort_abort_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """click.exceptions.Abort must NOT propagate out of _prompt_item."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(
        click,
        "prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(click.exceptions.Abort()),
    )

    # Must not raise
    try:
        _prompt_item(2, 5, "Schedule follow-up call", "w")
    except click.exceptions.Abort:
        pytest.fail("click.exceptions.Abort propagated out of _prompt_item")


# ── TestPromptItemEOFError (flattened) ──────────────────────────────────────


def test_prompt_item_eof_error_eoferror_returns_q(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOFError from click.prompt must be caught and return 'q'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(
        click,
        "prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()),
    )

    result = _prompt_item(1, 2, "Book the demo environment", "s")
    assert result == "q"


def test_prompt_item_eof_error_eoferror_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOFError must NOT propagate out of _prompt_item."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(
        click,
        "prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(EOFError()),
    )

    try:
        _prompt_item(3, 3, "Update the SOW", "w")
    except EOFError:
        pytest.fail("EOFError propagated out of _prompt_item")


# ── TestPromptItemHappyPath (flattened) ─────────────────────────────────────


def test_prompt_item_happy_path_valid_choice_m_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid input 'm' must be returned as 'm'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "m")

    result = _prompt_item(1, 1, "Send the contract", "m")
    assert result == "m"


def test_prompt_item_happy_path_valid_choice_w_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid input 'w' must be returned as 'w'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "w")

    result = _prompt_item(1, 1, "Waiting on legal review", "w")
    assert result == "w"


def test_prompt_item_happy_path_valid_choice_s_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid input 's' must be returned as 's'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "s")

    result = _prompt_item(1, 1, "General discussion item", "s")
    assert result == "s"


def test_prompt_item_happy_path_valid_choice_q_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid input 'q' must be returned as 'q'."""
    from fieldkit.commands.ingest.promote import _prompt_item

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "q")

    result = _prompt_item(1, 1, "Some item", "s")
    assert result == "q"


def test_prompt_item_happy_path_invalid_then_valid_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid input triggers re-prompt; second valid input is returned."""
    from fieldkit.commands.ingest.promote import _prompt_item

    responses = iter(["z", "m"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(responses))

    result = _prompt_item(1, 1, "Some item", "m")
    assert result == "m"


# ── TestPromptItemQuitPropagation (flattened) ───────────────────────────────


def test_prompt_item_quit_propagation_abort_causes_promote_file_to_return_true(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_promote_file must return True (quit) when _prompt_item returns 'q' via Abort."""
    from fieldkit.commands.ingest.promote import _promote_file

    # Create a minimal meeting note with one action item
    meeting = tmp_path / "2026-05-01-meeting.md"  # type: ignore[operator]
    meeting.write_text(  # type: ignore[union-attr]
        "---\n"
        "meeting_title: Test Meeting\n"
        "meeting_date: '2026-05-01'\n"
        "account: acme\n"
        "pursuits: []\n"
        "action_items:\n"
        "  - Send the proposal\n"
        "---\n"
        "# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"  # type: ignore[operator]
    tasks.write_text("## Active\n", encoding="utf-8")  # type: ignore[union-attr]

    # Simulate Abort on the first prompt call
    monkeypatch.setattr(
        click,
        "prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(click.exceptions.Abort()),
    )

    quit_early = _promote_file(
        meeting,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
        user_name="Test User",
        user_email="test@example.com",  # pii-guard: ignore
    )

    assert quit_early is True, "_promote_file must return True when user quits via Abort"


# ---------------------------------------------------------------------------
# Task 6.1 — _promote_file returns False when no action_items in frontmatter
# ---------------------------------------------------------------------------


# ── TestPromoteFileNoActionItems (flattened) ────────────────────────────────


def test_promote_file_no_action_items_promote_file_returns_false_when_no_action_items(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Meeting note with empty action_items list must return False (not quit early)."""
    from fieldkit.commands.ingest.promote import _promote_file

    meeting = tmp_path / "2026-06-01-no-actions.md"  # type: ignore[operator]
    meeting.write_text(  # type: ignore[union-attr]
        "---\n"
        "meeting_title: Empty Meeting\n"
        "meeting_date: '2026-06-01'\n"
        "account: acme\n"
        "pursuits: []\n"
        "action_items: []\n"
        "---\n"
        "# Notes\n"
        "Nothing to do.\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"  # type: ignore[operator]
    tasks.write_text("## Active\n", encoding="utf-8")  # type: ignore[union-attr]

    result = _promote_file(
        meeting,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
        user_name="Test User",
        user_email="test@example.com",  # pii-guard: ignore
    )

    assert result is False, "_promote_file must return False when there are no action items"


# ---------------------------------------------------------------------------
# Task 6.2 — _promote_file returns False on normal completion
# Task 6.2b — _promote_file returns True when user quits early
# ---------------------------------------------------------------------------


# ── TestPromoteFileCompletionSemantics (flattened) ──────────────────────────


def _promote_file_completion_semantics_make_meeting(tmp_path: pytest.TempPathFactory) -> tuple[object, object]:
    """Helper: create a meeting note with one action item and a TASKS.md."""
    meeting = tmp_path / "2026-06-02-meeting.md"  # type: ignore[operator]
    meeting.write_text(  # type: ignore[union-attr]
        "---\n"
        "meeting_title: Sprint Review\n"
        "meeting_date: '2026-06-02'\n"
        "account: acme\n"
        "pursuits: []\n"
        "action_items:\n"
        "  - Send the follow-up email\n"
        "---\n"
        "# Notes\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "TASKS.md"  # type: ignore[operator]
    tasks.write_text("## Active\n", encoding="utf-8")  # type: ignore[union-attr]
    return meeting, tasks


def test_promote_file_completion_semantics_promote_file_returns_false_on_normal_completion(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_promote_file returns False when user picks 'm' and all items are processed."""
    from fieldkit.commands.ingest import promote as promote_mod
    from fieldkit.commands.ingest.promote import _promote_file

    meeting, tasks = _promote_file_completion_semantics_make_meeting(tmp_path)

    # _prompt_item returns "m" — user claims the task
    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "m")
    # _write_active is a no-op so we don't need a real TASKS.md structure
    monkeypatch.setattr(promote_mod, "_write_active", lambda *args, **kwargs: None)

    result = _promote_file(
        meeting,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
        user_name="Test User",
        user_email="test@example.com",  # pii-guard: ignore
    )

    assert result is False, "_promote_file must return False after normal completion"


def test_promote_file_completion_semantics_promote_file_returns_true_on_quit_early(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_promote_file returns True when _prompt_item returns 'q' (quit early)."""
    from fieldkit.commands.ingest import promote as promote_mod
    from fieldkit.commands.ingest.promote import _promote_file

    meeting, tasks = _promote_file_completion_semantics_make_meeting(tmp_path)

    # _prompt_item returns "q" — user quits immediately
    monkeypatch.setattr(promote_mod, "_prompt_item", lambda *args, **kwargs: "q")

    result = _promote_file(
        meeting,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
        user_name="Test User",
        user_email="test@example.com",  # pii-guard: ignore
    )

    assert result is True, "_promote_file must return True when user quits early"


# ---------------------------------------------------------------------------
# Task 6.3 — _suggest return value tests
# ---------------------------------------------------------------------------


# ── TestSuggest (flattened) ─────────────────────────────────────────────────


def test_suggest_suggest_returns_m_for_owner_match() -> None:
    """_suggest returns 'm' when the item owner prefix matches the user's name."""
    from fieldkit.commands.ingest.promote import _suggest

    # Item starts with "Alice:" — matches user_name "Alice Smith"
    result = _suggest("Alice: Send the contract to legal", "Alice Smith", "alice@example.com")  # pii-guard: ignore

    assert result == "m", f"Expected 'm' for owner match, got {result!r}"


def test_suggest_suggest_returns_w_for_high_confidence_keyword() -> None:
    """_suggest returns 'w' when the item contains a high-confidence waiting-on keyword."""
    from fieldkit.commands.ingest.promote import _suggest

    # "docusign" is in _HIGH_CONFIDENCE_RE — no owner prefix so not 'm'
    result = _suggest(
        "Customer needs to sign the docusign envelope",
        "Bob Jones",
        "bob@example.com",  # pii-guard: ignore
    )  # pii-guard: ignore

    assert result == "w", f"Expected 'w' for high-confidence keyword, got {result!r}"


def test_suggest_suggest_returns_s_for_unclassifiable() -> None:
    """_suggest returns 's' when the item has no owner match and no keyword signal."""
    from fieldkit.commands.ingest.promote import _suggest

    # Generic discussion note — no owner prefix, no high-confidence keywords
    result = _suggest(
        "Team discussed the roadmap priorities for next quarter",
        "Carol White",
        "carol@example.com",  # pii-guard: ignore
    )  # pii-guard: ignore

    assert result == "s", f"Expected 's' for unclassifiable item, got {result!r}"


# ---------------------------------------------------------------------------
# Task 6.4 — CLI raises on no file and no --recent
# Task 6.5 — CLI raises on missing meeting file path
# ---------------------------------------------------------------------------


# ── TestPromoteCLIErrorPaths (flattened) ────────────────────────────────────


def test_promote_cli_error_paths_cli_raises_on_no_file_no_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    """promote with no args exits non-zero (missing FILE and --recent)."""
    from click.testing import CliRunner

    from fieldkit.commands.ingest.promote import cli

    runner = CliRunner()
    result = runner.invoke(cli, [])

    assert result.exit_code != 0, "promote with no args must exit non-zero"


def test_promote_cli_error_paths_cli_raises_on_missing_meeting_file(tmp_path: pytest.TempPathFactory) -> None:
    """promote with a nonexistent path exits non-zero (Click path validation)."""
    from click.testing import CliRunner

    from fieldkit.commands.ingest.promote import cli

    nonexistent = str(tmp_path / "does-not-exist.md")  # type: ignore[operator]
    runner = CliRunner()
    result = runner.invoke(cli, [nonexistent])

    assert result.exit_code != 0, "promote with a nonexistent file must exit non-zero"
