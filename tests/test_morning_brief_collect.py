"""Unit tests for fieldkit/morning_brief/collect.py and fieldkit/watch/morning_brief_collect.py.

Covers the five public collectors and the _extract_section helper embedded in
collect_tasks() (from morning_brief/collect.py), as well as calendar text-format
parsing and all-day event filtering (from watch/morning_brief_collect.py — historic regression).

Also covers extract_pursuit_summary() (implementation note, implementation note) and the D1a debug log
in the pursuit-file parse loop (implementation note).

All tests are isolated via tmp_path and targeted mocks — no real gmail.db,
Salesforce, or LLM calls are made.
"""

import logging
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.brief.collect as collect_mod
from fieldkit.commands.brief.collect import (
    _champion_signal_block,
    collect_decay_signals,
    collect_pipeline_pulse,
    collect_pursuit_alerts,
    collect_tasks,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_pursuit(path: Path, stage: str = "propose", extra_frontmatter: str = "") -> None:
    """Write a minimal valid pursuit file to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: {stage}\n{extra_frontmatter}---\n# Pursuit\n",
        encoding="utf-8",
    )


def _pursuits_dir(data_root: Path, account: str = "acme-corp") -> Path:
    """Return (and create) the pursuits directory for *account*."""
    d = data_root / "accounts" / account / "pursuits"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_pursuit_alerts_returns_empty_when_no_accounts(tmp_path: Path) -> None:
    """collect_pursuit_alerts returns the no-alerts sentinel when accounts/ is absent.

    The function calls iterate_pursuits() which globs accounts/*/pursuits/*.md.
    With no accounts directory the glob yields nothing, so the result must be
    the "No active pursuits flagged today." sentinel — not an empty string or
    an exception.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()
    # Deliberately do NOT create data_root/accounts/

    result = collect_pursuit_alerts(data_root)

    assert result == "No active pursuits flagged today."


@pytest.mark.unit
def test_collect_pursuit_alerts_returns_formatted_section(tmp_path: Path) -> None:
    """Historical local gaps are replaced by native availability status."""
    data_root = tmp_path / "data"
    pursuits = _pursuits_dir(data_root, "acme-corp")

    # Pursuit with no champion, no economic buyer, no last_transition.
    # All 8 MEDDPICC fields are required by MEDDPICCScore (paper-process is the 8th).
    _write_pursuit(
        pursuits / "big-deal.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 1\n"
            "  economic-buyer: 0\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  identify-pain: 1\n"
            "  champion: 0\n"
            "  competition: 1\n"
            "  paper-process: 1\n"
        ),
    )

    result = collect_pursuit_alerts(data_root)

    assert "acme-corp" in result
    assert "big-deal" in result
    assert "Native qualification: unavailable" in result
    assert "No Champion" not in result
    assert "No Economic Buyer" not in result


@pytest.mark.unit
def test_collect_pipeline_pulse_returns_red_for_near_close(tmp_path: Path) -> None:
    """collect_pipeline_pulse marks a pursuit RED when close date ≤ 30 days away.

    Patches date.today() inside the collect module so the close date is
    deterministically within the RED window regardless of when the test runs.
    """
    data_root = tmp_path / "data"
    pursuits = _pursuits_dir(data_root, "acme-corp")

    # Close date 10 days from the mocked "today" → RED
    fake_today = date(2026, 6, 14)
    close_date = date(2026, 6, 24).isoformat()  # 10 days away

    _write_pursuit(
        pursuits / "urgent-deal.md",
        stage="propose",
        extra_frontmatter=f"sf_close_date: {close_date}\n",
    )

    # collect.py reads the day as datetime.now(tz=UTC).date(), so that is the
    # symbol to freeze. Patching `date` here would be inert (historic regression), and
    # leaving `date` real keeps date.fromisoformat working for the parse branch.
    with patch.object(collect_mod, "datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(fake_today.year, fake_today.month, fake_today.day, tzinfo=UTC)
        result = collect_pipeline_pulse(data_root)

    assert "🔴" in result, f"Expected RED flag in output, got:\n{result}"
    assert "urgent-deal" in result


@pytest.mark.unit
def test_collect_pipeline_pulse_returns_sentinel_when_no_active(tmp_path: Path) -> None:
    """collect_pipeline_pulse returns the no-pursuits sentinel when accounts/ is absent.

    With no pursuit files the entries list is empty and the function must
    return "No active pursuits found." rather than an empty string or raise.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()
    # No accounts directory → iterate_pursuits yields nothing

    result = collect_pipeline_pulse(data_root)

    assert result == "No active pursuits found."


@pytest.mark.unit
def test_collect_decay_signals_returns_sentinel_when_no_gmail_db(tmp_path: Path) -> None:
    """collect_decay_signals returns the unavailable sentinel when gmail.db is absent.

    Patches _gmail_db_exists() to return False (simulating a missing gmail.db)
    so the function short-circuits before touching the filesystem or any DB.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()

    with patch.object(collect_mod, "_gmail_db_exists", return_value=False):
        result = collect_decay_signals(data_root)

    assert "gmail.db not found" in result
    assert "unavailable" in result


@pytest.mark.unit
def test_champion_signal_block_handles_missing_champion_name(tmp_path: Path) -> None:
    """_champion_signal_block returns None when no champion name can be extracted.

    When account.md is absent (or has no **Champion:** line) extract_champion_name
    returns '' and the function must return None without raising.
    """
    import sqlite3

    data_root = tmp_path / "data"
    pursuits = _pursuits_dir(data_root, "acme-corp")

    # Pursuit with a champion MEDDPICC score > 0 so the champion-name branch is reached.
    # All 8 MEDDPICC fields are required by MEDDPICCScore (paper-process is the 8th).
    _write_pursuit(
        pursuits / "deal.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 1\n"
            "  economic-buyer: 1\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  identify-pain: 1\n"
            "  champion: 2\n"
            "  competition: 1\n"
            "  paper-process: 1\n"
        ),
    )
    # No account.md → extract_champion_name returns ''
    account_dir = data_root / "accounts" / "acme-corp"
    account_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(":memory:")
    try:
        result = _champion_signal_block(
            conn,
            data_root,
            "acme-corp",
            pursuits / "deal.md",
        )
    finally:
        conn.close()

    assert result is None, f"Expected None when champion name is absent, got: {result!r}"


@pytest.mark.unit
def test_collect_tasks_extract_section_returns_none_when_absent(tmp_path: Path) -> None:
    """collect_tasks returns the default sentinel when the Today section is absent.

    TASKS.md exists but contains no '## Today' heading.  The _extract_section
    helper (closure inside collect_tasks) must return None, causing collect_tasks
    to substitute the default sentinel string.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()
    tasks_file = data_root / "TASKS.md"
    tasks_file.write_text(
        "# Tasks\n\n## Backlog\n\n- some future item\n",
        encoding="utf-8",
    )

    today_section, waiting_section = collect_tasks(data_root)

    assert today_section == "(nothing committed for today yet)"
    assert waiting_section == "(nothing in the queue)"


@pytest.mark.unit
def test_collect_tasks_extract_section_returns_content_when_present(tmp_path: Path) -> None:
    """collect_tasks returns section content when Today and Waiting On are present.

    TASKS.md has both headings with real content.  collect_tasks must return
    the lines from each section (up to 10) without the heading itself.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()
    tasks_file = data_root / "TASKS.md"
    tasks_file.write_text(
        "# Tasks\n\n"
        "## Today\n\n"
        "- [ ] Send proposal to Acme\n"
        "- [ ] Follow up with champion\n\n"
        "## Waiting On\n\n"
        "- [ ] Legal review from Acme\n\n"
        "## Backlog\n\n"
        "- future item\n",
        encoding="utf-8",
    )

    today_section, waiting_section = collect_tasks(data_root)

    assert "Send proposal to Acme" in today_section
    assert "Follow up with champion" in today_section
    assert "Legal review from Acme" in waiting_section
    # Backlog items must not bleed into either section
    assert "future item" not in today_section
    assert "future item" not in waiting_section


# ── historic regression: Calendar text-format parsing ────────────────────────────────────


def test_retired_command_layer_collection_module_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module("fieldkit.commands.watch.morning_brief_collect")

    assert exc_info.value.name == "fieldkit.commands.watch.morning_brief_collect"


# ── TestParseCalendarText (flattened) ───────────────────────────────────────


def test_parse_calendar_text_timed_event_uses_dateTime_key() -> None:
    """A calendar entry with a time component must use the dateTime key."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_text

    text = (
        "Successfully retrieved 1 event from calendar primary:\n"
        ' - "Team Standup" (Starts: 2026-06-18T09:00:00, Ends: 2026-06-18T09:30:00)\n'
    )
    events = _parse_calendar_text(text)
    assert len(events) == 1
    ev = events[0]
    assert "dateTime" in ev["start"], "Timed event must use dateTime key, not date"
    assert "T" in ev["start"]["dateTime"]
    assert "dateTime" in ev["end"]


def test_parse_calendar_text_all_day_event_uses_date_key() -> None:
    """A date-only calendar entry (no time) must use the date key."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_text

    text = 'Successfully retrieved 1 event from calendar primary:\n - "OOO" (Starts: 2026-06-18, Ends: 2026-06-19)\n'
    events = _parse_calendar_text(text)
    assert len(events) == 1
    ev = events[0]
    assert "date" in ev["start"], "Date-only event must use date key"
    assert "dateTime" not in ev["start"]


def test_parse_calendar_text_timed_text_event_not_filtered_as_all_day() -> None:
    """historic regression: timed text-format events must NOT be dropped by the all-day filter."""

    from fieldkit.watch.morning_brief_collect import _parse_calendar_text, fetch_external_meetings

    # Build a text-format timed event directly
    text = (
        "Successfully retrieved 1 event from calendar primary:\n"
        ' - "Customer Call" (Starts: 2026-06-18T14:00:00, Ends: 2026-06-18T15:00:00)\n'
    )
    events = _parse_calendar_text(text)
    assert len(events) == 1
    # dateTime key is set — all-day filter must pass this through
    ev = events[0]
    assert "dateTime" in ev["start"]

    # Now test the full fetch_external_meetings filter path with a mock session
    session_mock = MagicMock()
    session_mock.call_tool.return_value = text

    with patch("fieldkit.watch.morning_brief_collect._parse_calendar_result", return_value=events):
        result = fetch_external_meetings(
            session=session_mock,
            target_date=date(2026, 6, 18),
            internal_domains={"your-org.com"},
            user_email="test@example.com",  # pii-guard: ignore
        )
    # The meeting must appear — it is timed, not an all-day block.
    # Also verify the start time flows through correctly (contains T → timed event).
    assert len(result) == 1
    assert result[0]["title"] == "Customer Call"
    assert "T" in result[0]["start"], "start value must contain T (time component) for a timed event"
    # Contract: must return list[dict] with required keys
    assert isinstance(result, list)
    assert "title" in result[0]
    assert "start" in result[0]


def test_parse_calendar_text_all_day_text_event_is_filtered() -> None:
    """All-day text-format events (date-only start) must still be skipped."""

    from fieldkit.watch.morning_brief_collect import _parse_calendar_text, fetch_external_meetings

    text = (
        "Successfully retrieved 1 event from calendar primary:\n"
        ' - "Personal OOO" (Starts: 2026-06-18, Ends: 2026-06-19)\n'
    )
    events = _parse_calendar_text(text)
    assert len(events) == 1
    assert "date" in events[0]["start"]

    session_mock = MagicMock()
    with patch("fieldkit.watch.morning_brief_collect._parse_calendar_result", return_value=events):
        result = fetch_external_meetings(
            session=session_mock,
            target_date=date(2026, 6, 18),
            internal_domains={"your-org.com"},
            user_email="test@example.com",  # pii-guard: ignore
        )
    # All-day events must be filtered out
    assert len(result) == 0


def test_parse_calendar_text_fetch_external_meetings_empty_email_raises() -> None:
    """fetch_external_meetings raises RuntimeError when user_email is empty."""
    from datetime import date as _date

    from fieldkit.watch.morning_brief_collect import fetch_external_meetings

    session_mock = MagicMock()
    with pytest.raises(RuntimeError, match="user_google_email is required"):
        fetch_external_meetings(
            session=session_mock,
            target_date=_date(2026, 6, 18),
            internal_domains=set(),
            user_email="",
        )


# ── implementation note/implementation note: extract_pursuit_summary uses load_pursuit() ────────────────


def _write_pursuit_file(path: Path, frontmatter: str) -> None:
    """Write a minimal pursuit file with the given frontmatter block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n# Pursuit body\n", encoding="utf-8")


# ── TestExtractPursuitSummary (flattened) ───────────────────────────────────


def test_extract_pursuit_summary_null_acv_normalises_to_empty_string(tmp_path: Path) -> None:
    """sf_acv: null in frontmatter must produce acv='' in the result, not 'None'.

    Regression for implementation note: raw YAML dict access returned None which was then
    str()-coerced to 'None', polluting the brief output.
    """
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "big-deal.md"
    _write_pursuit_file(
        pursuit,
        "stage: propose\nsf_acv: null\n",
    )

    with patch(
        "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
        return_value=tmp_path,
    ):
        result = extract_pursuit_summary(pursuit)

    assert result is not None
    assert result["acv"] == "", f"Expected empty string for null sf_acv, got {result['acv']!r}"


def test_extract_pursuit_summary_none_string_next_steps_normalises_to_empty_string(tmp_path: Path) -> None:
    """sf_next_steps: 'None' (literal string) must produce next_steps='' in result.

    Legacy files may contain the literal string 'None' written by old code
    that str()-coerced a Python None.  The function must normalise this to ''.
    """
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "big-deal.md"
    _write_pursuit_file(
        pursuit,
        'stage: propose\nsf_next_steps: "None"\n',
    )

    with patch(
        "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
        return_value=tmp_path,
    ):
        result = extract_pursuit_summary(pursuit)

    assert result is not None
    assert result["next_steps"] == "", f"Expected empty string for sf_next_steps='None', got {result['next_steps']!r}"


def test_extract_pursuit_summary_malformed_frontmatter_returns_none_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file with invalid YAML frontmatter must return None and log a debug message.

    implementation note: load_pursuit() raises ValueError for missing/malformed frontmatter.
    extract_pursuit_summary must catch it and return None rather than propagating.
    """
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "bad.md"
    pursuit.parent.mkdir(parents=True, exist_ok=True)
    # No frontmatter delimiters — load_pursuit raises ValueError
    pursuit.write_text("Just plain text, no frontmatter at all.\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="fieldkit.watch"):
        result = extract_pursuit_summary(pursuit)

    assert result is None, f"Expected None for malformed pursuit, got {result!r}"
    assert any("Skipping unparseable" in r.message for r in caplog.records), (
        "Expected 'Skipping unparseable' in debug log"
    )


# ── implementation note: D1a debug log in silent except Exception block ────────────────────


# ── TestD1aDebugLog (flattened) ─────────────────────────────────────────────


def test_d1a_debug_log_parse_error_in_loop_logs_filename(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A pursuit file that triggers an exception in the parse loop must be logged.

    Patches parse_frontmatter to raise RuntimeError for a specific file so
    the except Exception branch is exercised without needing a truly malformed
    file (parse_frontmatter already handles YAML errors internally).
    """
    from fieldkit.watch.morning_brief_collect import get_latest_pursuit_files

    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "broken.md"
    _write_pursuit_file(pursuit, "stage: propose\n")

    with (
        patch(
            "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
            return_value=tmp_path,
        ),
        patch(
            "fieldkit.watch.morning_brief_collect.parse_frontmatter",
            side_effect=RuntimeError("simulated parse failure"),
        ),
        caplog.at_level(logging.DEBUG, logger="fieldkit.watch"),
    ):
        get_latest_pursuit_files(n=5)

    assert any("broken.md" in r.message for r in caplog.records), (
        f"Expected 'broken.md' in debug log. Records: {[r.message for r in caplog.records]}"
    )


@pytest.mark.unit
def test_get_latest_pursuit_files_logs_unparseable_close_date(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An invalid close date is retained and logged without failing collection."""
    from fieldkit.watch.morning_brief_collect import get_latest_pursuit_files

    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "bad-close-date.md"
    _write_pursuit_file(pursuit, "stage: propose\nsf_close_date: not-a-date\n")

    with (
        patch("fieldkit.watch.morning_brief_collect.get_fieldkit_home", return_value=tmp_path),
        caplog.at_level(logging.DEBUG, logger="fieldkit.watch"),
    ):
        result = get_latest_pursuit_files(n=5)

    assert result == [pursuit]
    assert any(
        "Unparseable close_date_str" in record.message and record.levelno == logging.DEBUG for record in caplog.records
    )


# ── implementation note D1b: debug log on unparseable sf_close_date ───────────────────────


@pytest.mark.unit
def test_collect_pipeline_pulse_logs_debug_on_bad_close_date(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D1b (implementation note): silent ValueError/TypeError on bad sf_close_date must emit a DEBUG log.

    Regression guard: the ``except (ValueError, TypeError): pass`` block in
    collect_pipeline_pulse() was replaced with a ``log.debug(...)`` call.
    This test verifies:
    1. No exception propagates to the caller.
    2. A DEBUG record containing "Unparseable sf_close_date" is emitted.
    """
    data_root = tmp_path / "data"
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuits_dir.mkdir(parents=True, exist_ok=True)

    # Write a pursuit with an unparseable close date
    pursuit_file = pursuits_dir / "bad-close-date.md"
    pursuit_file.write_text(
        "---\nstage: propose\nsf_close_date: 'not-a-date'\n---\n# Pursuit\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="fieldkit.commands.brief.collect"):
        result = collect_pipeline_pulse(data_root)

    # No exception raised — function returns a string
    assert isinstance(result, str)

    debug_messages = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "Unparseable sf_close_date" in debug_messages, (
        f"Expected 'Unparseable sf_close_date' in DEBUG log. Records: {[r.message for r in caplog.records]}"
    )
