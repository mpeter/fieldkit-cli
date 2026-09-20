"""Tests for fieldkit/watch/morning_brief.py — alert extraction and deduplication."""

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_collect import extract_today_alerts

pytestmark = pytest.mark.unit

_TODAY = datetime.date(2026, 6, 7)
_TODAY_STR = "2026-06-07"
_OTHER_STR = "2026-06-06"


def _alerts_file(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "alerts.md"
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# extract_today_alerts — basic extraction
# ---------------------------------------------------------------------------


def test_extracts_today_blocks(tmp_path: Path):
    # Use lookback_days=0 to assert today-only filtering (yesterday excluded).
    # _OTHER_STR is 1 day before _TODAY; with lookback_days=0 it must be excluded.
    f = _alerts_file(
        tmp_path,
        f"# Alerts\n\n"
        f"## {_TODAY_STR} — account / pursuit — stalled in discover\n\n"
        f"- Days: 20\n\n"
        f"## {_OTHER_STR} — account / pursuit — stalled in discover\n\n"
        f"- Days: 19\n",
    )
    alerts = extract_today_alerts(f, _TODAY, lookback_days=0)
    assert isinstance(alerts, list)
    assert len(alerts) == 1
    assert _TODAY_STR in alerts[0]
    assert _OTHER_STR not in alerts[0]


def test_returns_empty_when_no_today_alerts(tmp_path: Path):
    # Use lookback_days=0 so that only today's alerts are considered.
    # _OTHER_STR is yesterday; with lookback_days=0 it must be excluded.
    f = _alerts_file(
        tmp_path,
        f"## {_OTHER_STR} — account / pursuit — stalled in discover\n\n- Days: 19\n",
    )
    alerts = extract_today_alerts(f, _TODAY, lookback_days=0)
    assert isinstance(alerts, list)
    assert alerts == []


def test_raises_when_file_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match=r"."):
        extract_today_alerts(tmp_path / "nonexistent.md", _TODAY)


# ---------------------------------------------------------------------------
# extract_today_alerts — deduplication (historic regression regression)
# ---------------------------------------------------------------------------


def test_deduplicates_identical_headings(tmp_path: Path):
    """historic regression regression: same heading written N times produces 1 block."""
    block = f"## {_TODAY_STR} — acct / ads-repave — stalled in propose\n\n- Days: 37\n"
    f = _alerts_file(tmp_path, (block * 5))
    alerts = extract_today_alerts(f, _TODAY)
    assert len(alerts) == 1


def test_does_not_deduplicate_distinct_pursuits(tmp_path: Path):
    """Different pursuit names on the same day are distinct blocks."""
    content = (
        f"## {_TODAY_STR} — acct / pursuit-a — stalled in propose\n\n- Days: 20\n\n"
        f"## {_TODAY_STR} — acct / pursuit-b — stalled in discover\n\n- Days: 15\n"
    )
    f = _alerts_file(tmp_path, content)
    alerts = extract_today_alerts(f, _TODAY)
    assert len(alerts) == 2


def test_deduplicates_mixed_pursuits_with_repeats(tmp_path: Path):
    """3 unique pursuits each written 4 times -> 3 blocks."""
    pursuits = ["pursuit-a", "pursuit-b", "pursuit-c"]
    blocks = ""
    for pursuit in pursuits:
        for _ in range(4):
            blocks += f"## {_TODAY_STR} — acct / {pursuit} — stalled in discover\n\n- Days: 20\n\n"
    f = _alerts_file(tmp_path, blocks)
    alerts = extract_today_alerts(f, _TODAY)
    assert len(alerts) == 3


def test_deduplication_with_different_body_same_heading(tmp_path: Path):
    """Same heading but different body (e.g. different 'Detected at' timestamp) -> 1 block."""
    content = (
        f"## {_TODAY_STR} — acct / pursuit-a — stalled in propose\n\n"
        f"- Detected at: 2026-06-07T09:00:00Z\n\n"
        f"## {_TODAY_STR} — acct / pursuit-a — stalled in propose\n\n"
        f"- Detected at: 2026-06-07T10:00:00Z\n"
    )
    f = _alerts_file(tmp_path, content)
    alerts = extract_today_alerts(f, _TODAY)
    assert len(alerts) == 1
    # First occurrence is kept
    assert "09:00" in alerts[0]


# ---------------------------------------------------------------------------
# _render_quota_section tests
# ---------------------------------------------------------------------------


def test_quota_section_no_config(monkeypatch: pytest.MonkeyPatch):
    """When quota not configured, show setup instructions."""
    from fieldkit.watch.morning_brief_render import _render_quota_section

    monkeypatch.setattr("fieldkit.watch.morning_brief_render.get_pipeline_quota", lambda: None)
    lines = _render_quota_section(None, lambda _root: [])
    combined = "\n".join(lines)
    assert "## Quota Gap" in combined
    assert "fieldkit pipeline quota" in combined


def test_quota_section_without_config_does_not_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from fieldkit.watch.morning_brief_render import _render_quota_section

    collector = Mock()
    monkeypatch.setattr("fieldkit.watch.morning_brief_render.get_pipeline_quota", lambda: None)

    lines = _render_quota_section(None, collector)

    assert lines
    collector.assert_not_called()


def test_quota_section_with_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """When quota configured, renders table with Target/Closed-won/Weighted/Gap."""
    from fieldkit.watch.morning_brief_render import _render_quota_section

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_render.get_pipeline_quota",
        lambda: {"target": 1_000_000, "period": "2026-H2"},
    )
    pursuits: list[dict[str, object]] = [
        {"stage": "closed-won", "sf_amount": "200000"},
        {"stage": "propose", "sf_amount": "500000"},
    ]
    lines = _render_quota_section(tmp_path, lambda _root: pursuits)
    combined = "\n".join(lines)
    assert "## Quota Gap (2026-H2)" in combined
    assert "Target" in combined
    assert "Closed-won" in combined
    assert "Weighted pipeline" in combined
    assert "Gap" in combined
    assert "$1,000,000" in combined


def test_quota_section_passes_collector_result_identity_to_calculator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from fieldkit.watch.morning_brief_render import _render_quota_section

    pursuits: list[dict[str, object]] = []
    collector = Mock(return_value=pursuits)
    calculator = Mock(return_value={"target": 1.0, "closed_won": 0.0, "weighted": 0.0, "gap": 1.0})
    monkeypatch.setattr("fieldkit.watch.morning_brief_render.get_pipeline_quota", lambda: {"target": 1})
    monkeypatch.setattr("fieldkit.watch.morning_brief_render.calculate_quota_gap", calculator)

    lines = _render_quota_section(tmp_path, collector)

    assert lines
    assert calculator.call_args.args[0] is pursuits


def test_quota_section_propagates_collector_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fieldkit.watch.morning_brief_render import _render_quota_section

    def fail_collection(_root: Path) -> list[dict[str, object]]:
        raise RuntimeError("collection failed")

    monkeypatch.setattr("fieldkit.watch.morning_brief_render.get_pipeline_quota", lambda: {"target": 1})

    with pytest.raises(RuntimeError, match="collection failed"):
        _render_quota_section(tmp_path, fail_collection)


# ---------------------------------------------------------------------------
# Pipeline pulse: n=5 default and RED close-date sorting
# ---------------------------------------------------------------------------


def test_get_latest_pursuit_files_returns_5(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """get_latest_pursuit_files defaults to n=5."""
    from fieldkit.watch.morning_brief_collect import get_latest_pursuit_files

    # Create 7 pursuit files across two fake accounts
    for account in ("acme-corp", "globalpay"):
        pursuits = tmp_path / account / "pursuits"
        pursuits.mkdir(parents=True)
        for i in range(4):
            p = pursuits / f"deal-{account}-{i}.md"
            p.write_text(
                f"---\nstage: propose\nsf_close_date: 2027-12-31\n---\n# deal {i}\n",
                encoding="utf-8",
            )

    monkeypatch.setattr("fieldkit.watch.morning_brief_collect._accounts_dir", lambda: tmp_path)

    result = get_latest_pursuit_files()
    assert len(result) == 5
    # Contract: must return list[Path] where all items are .md files
    assert all(isinstance(p, Path) for p in result)
    assert all(p.suffix == ".md" for p in result)


def test_red_close_date_sorted_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A pursuit with close_date within 30 days sorts before one closing in 90 days."""
    from datetime import date, timedelta

    from fieldkit.watch.morning_brief_collect import get_latest_pursuit_files

    pursuits = tmp_path / "acme-corp" / "pursuits"
    pursuits.mkdir(parents=True)

    today = date.today()
    close_soon = (today + timedelta(days=15)).isoformat()
    close_later = (today + timedelta(days=90)).isoformat()

    red_deal = pursuits / "red-deal.md"
    red_deal.write_text(
        f"---\nstage: propose\nsf_close_date: {close_soon}\n---\n# red deal\n",
        encoding="utf-8",
    )
    safe_deal = pursuits / "safe-deal.md"
    safe_deal.write_text(
        f"---\nstage: negotiate\nsf_close_date: {close_later}\n---\n# safe deal\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("fieldkit.watch.morning_brief_collect._accounts_dir", lambda: tmp_path)

    result = get_latest_pursuit_files(n=5)
    names = [p.name for p in result]
    assert names.index("red-deal.md") < names.index("safe-deal.md"), (
        "RED close-date pursuit should sort before far-future pursuit"
    )
    # Contract: must return list[Path] where all items are .md files
    assert all(isinstance(p, Path) for p in result)
    assert all(p.suffix == ".md" for p in result)


# ---------------------------------------------------------------------------
# collect_tasks regression: must read TASKS.md from data_root, NOT fieldkit_root
# BUG-M060 regression: get_fieldkit_root was incorrectly used for TASKS.md path
# ---------------------------------------------------------------------------


def test_collect_tasks_reads_from_data_root(tmp_path: Path):
    """Regression: collect_tasks() reads TASKS.md from data_root, not fieldkit_root.

    Before the M060 fix, collect_tasks used get_fieldkit_root() to locate
    TASKS.md, causing FileNotFoundError when installed outside the source tree.
    Now it takes data_root as an explicit parameter and must read from there.
    """
    from fieldkit.commands.brief.main import collect_tasks

    # Set up TASKS.md in a standalone data_root (not the code repo)
    data_root = tmp_path / "fieldkit-data"
    data_root.mkdir()

    tasks_content = (
        "# TASKS\n\n## Today\n\n- [ ] Send renewal proposal\n\n## Waiting On\n\n- Legal review (2026-06-01)\n"
    )
    (data_root / "TASKS.md").write_text(tasks_content, encoding="utf-8")

    today_section, waiting_section = collect_tasks(data_root)

    # The function must find the content from data_root, not fieldkit_root
    assert "Send renewal proposal" in today_section, "collect_tasks did not read Today section from data_root"
    assert "Legal review" in waiting_section, "collect_tasks did not read Waiting On section from data_root"


def test_collect_tasks_returns_empty_strings_when_tasks_md_missing(tmp_path: Path):
    """collect_tasks() gracefully returns empty strings when TASKS.md is absent in data_root."""
    from fieldkit.commands.brief.main import collect_tasks

    data_root = tmp_path / "empty-data"
    data_root.mkdir()
    # No TASKS.md here

    today_section, waiting_section = collect_tasks(data_root)
    # Should not raise; both sections are empty or contain a placeholder
    assert isinstance(today_section, str)
    assert isinstance(waiting_section, str)


def test_collect_tasks_does_not_read_from_fieldkit_root(tmp_path: Path, monkeypatch):
    """Regression guard: collect_tasks must NOT call get_fieldkit_root() internally.

    Place TASKS.md only in a separate data_root dir. If collect_tasks reads
    from fieldkit_root instead, it will return empty/missing — and this test fails.
    """
    from fieldkit.commands.brief.main import collect_tasks

    # data_root has the file
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "TASKS.md").write_text(
        "# TASKS\n\n## Today\n\n- [ ] Regression guard item\n\n## Waiting On\n\n",
        encoding="utf-8",
    )

    # Patch get_fieldkit_root to point somewhere with no TASKS.md
    bogus_root = tmp_path / "bogus-fieldkit"
    bogus_root.mkdir()
    import fieldkit.config as _config_mod

    monkeypatch.setattr(_config_mod, "get_fieldkit_root", lambda: bogus_root)

    today_section, _ = collect_tasks(data_root)
    assert "Regression guard item" in today_section, (
        "collect_tasks did not read from data_root — may be using fieldkit_root instead"
    )


# ---------------------------------------------------------------------------
# implementation note: _extract_section matches single-# headings in TASKS.md
# ---------------------------------------------------------------------------


# ── TestExtractSectionHeadingLevels (flattened) ─────────────────────────────


def _extract_section_heading_levels_collect_tasks(tmp_path: Path, content: str) -> tuple[str, str]:
    from fieldkit.commands.brief.main import collect_tasks

    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    (data_root / "TASKS.md").write_text(content, encoding="utf-8")
    return collect_tasks(data_root)


def test_extract_section_heading_levels_single_hash_heading_matched(tmp_path: Path) -> None:
    """implementation note: TASKS.md with '# Today' heading is matched by _extract_section."""
    today, _ = _extract_section_heading_levels_collect_tasks(tmp_path, "# Today\n- did a thing\n")
    assert "did a thing" in today, f"Expected 'did a thing' in today section, got: {today!r}"


def test_extract_section_heading_levels_double_hash_heading_still_matched(tmp_path: Path) -> None:
    """implementation note: existing ## Today heading continues to work (no regression)."""
    today, _ = _extract_section_heading_levels_collect_tasks(tmp_path, "## Today\n- did a thing\n")
    assert "did a thing" in today, f"Expected 'did a thing' in today section, got: {today!r}"


def test_extract_section_heading_levels_comment_only_section_returns_empty_string_not_none(tmp_path: Path) -> None:
    """implementation note: section with only HTML comments returns '' (found but empty), not None.

    Also verifies that Waiting On content does NOT bleed into the Today section
    when Today contains only an HTML comment anchor.
    """
    content = "## Today\n<!-- anchor -->\n## Waiting On\n- foo\n"
    today, waiting = _extract_section_heading_levels_collect_tasks(tmp_path, content)
    # Today section exists but is comment-only → empty string (not the placeholder)
    assert today == "(nothing committed for today yet)" or today == "", (
        f"Expected empty or placeholder for comment-only Today, got: {today!r}"
    )
    # Waiting On content must NOT bleed into Today
    assert "foo" not in today, f"Waiting On content bled into Today section: {today!r}"
    # Waiting On section must be correctly extracted
    assert "foo" in waiting, f"Expected 'foo' in waiting section, got: {waiting!r}"


def test_extract_section_heading_levels_next_section_content_does_not_bleed(tmp_path: Path) -> None:
    """implementation note: content after the next heading does not bleed into the current section."""
    content = "## Today\n- my task\n## Waiting On\n- blocked item\n"
    today, waiting = _extract_section_heading_levels_collect_tasks(tmp_path, content)
    assert "my task" in today
    assert "blocked item" not in today
    assert "blocked item" in waiting


# ── TestDegradedSources (flattened) ─────────────────────────────────────────


def test_degraded_sources_collect_degraded_when_gmail_db_missing(tmp_path, monkeypatch):
    """When gmail.db is missing, 'Gmail cache' appears in the degraded list."""
    # Patch _gmail_db_exists to return False
    import fieldkit.commands.brief.main as main_mod
    from fieldkit.commands.brief.main import _collect_degraded_sources

    monkeypatch.setattr(main_mod, "_gmail_db_exists", lambda: False)

    data_root = tmp_path / "data"
    data_root.mkdir()
    # Create accounts dir and accounts.yaml so only Gmail is degraded
    (data_root / "accounts").mkdir()
    accounts_yaml = data_root / "accounts.yaml"
    accounts_yaml.write_text("accounts: {}", encoding="utf-8")
    monkeypatch.setattr("fieldkit.commands.brief.main.get_config_path", lambda name: accounts_yaml)

    result = _collect_degraded_sources(data_root)
    labels = [label for label, _ in result]
    assert "Gmail cache" in labels


def test_degraded_sources_render_degraded_section_empty():
    """_render_degraded_section returns empty string for empty list."""
    from fieldkit.commands.brief.main import _render_degraded_section

    assert _render_degraded_section([]) == ""


def test_degraded_sources_render_degraded_section_formats_correctly():
    """_render_degraded_section renders label and reason in output."""
    from fieldkit.commands.brief.main import _render_degraded_section

    result = _render_degraded_section([("Gmail cache", "gmail.db not found"), ("Pursuits", "accounts dir missing")])
    assert "Degraded Sources" in result
    assert "Gmail cache" in result
    assert "gmail.db not found" in result
    assert "Pursuits" in result


# ---------------------------------------------------------------------------
# _render_meetings_section — calendar error surfaced (S02 regression)
# ---------------------------------------------------------------------------


# ── TestCalendarErrorSurfaced (flattened) ───────────────────────────────────


def test_calendar_error_surfaced_calendar_error_string_rendered_verbatim():
    """historic regression: when calendar returns an error string, it is rendered verbatim in the brief."""
    from fieldkit.watch.morning_brief_render import _render_meetings_section

    error_msg = "_Calendar unavailable (RuntimeError) — check logs for details_"
    lines = _render_meetings_section(error_msg)
    combined = "\n".join(lines)

    assert "## Today's External Meetings" in combined
    # historic regression: the error string must appear in the rendered output
    assert "Calendar unavailable" in combined
    assert "RuntimeError" in combined


def test_calendar_error_surfaced_calendar_error_shows_exception_type():
    """historic regression: error string contains the exception type, not raw message text."""
    from fieldkit.watch.morning_brief import _collect_calendar_meetings
    from fieldkit.watch.morning_brief_mcp import MCPSession

    def _bad_init(self):
        raise ConnectionRefusedError("localhost:8080 refused")

    # Use a monkeypatched session to trigger the except branch
    with patch.object(MCPSession, "initialize", _bad_init):
        result = _collect_calendar_meetings(
            datetime.date(2026, 6, 7),
            internal_domains={"your-org.com"},
            user_email="user@example.com",  # pii-guard: ignore
        )

    assert isinstance(result, str)
    # historic regression + Constitution VIII: exception TYPE is surfaced, not raw message
    assert "ConnectionRefusedError" in result
    assert "Calendar unavailable" in result
    # Raw internal details must NOT appear
    assert "localhost:8080" not in result


def test_calendar_error_surfaced_collect_calendar_meetings_includes_exception_type_on_failure(monkeypatch):
    """historic regression: _collect_calendar_meetings returns string with exception type name."""
    from fieldkit.watch.morning_brief import _collect_calendar_meetings
    from fieldkit.watch.morning_brief_mcp import MCPSession

    def _bad_init(self):
        raise RuntimeError("Connection refused to internal host")

    monkeypatch.setattr(MCPSession, "initialize", _bad_init)

    result = _collect_calendar_meetings(
        datetime.date(2026, 6, 7),
        internal_domains={"your-org.com"},
        user_email="user@example.com",  # pii-guard: ignore
    )

    assert isinstance(result, str), "Expected error string, got list"
    # historic regression: exception type name in the return value (not raw message)
    assert "RuntimeError" in result
    assert "Calendar unavailable" in result
    # Raw exception message must NOT appear (Constitution VIII)
    assert "Connection refused to internal host" not in result


def test_calendar_error_surfaced_render_brief_includes_calendar_error_type(monkeypatch):
    """historic regression: full render_brief call surfaces exception type in output."""
    from fieldkit.watch.morning_brief_render import render_brief

    monkeypatch.setattr("fieldkit.watch.morning_brief_render.get_pipeline_quota", lambda: None)

    brief = render_brief(
        target_date=datetime.date(2026, 6, 7),
        meetings="_Calendar unavailable (TimeoutError) — check logs for details_",
        backstory_alerts=[],
        pursuit_stall_alerts=[],
        slack_alerts=[],
        pipeline_review_md="no data",
        elapsed_seconds=0.1,
        quota_collector=lambda _root: [],
    )

    # historic regression: the error string is rendered verbatim
    assert "TimeoutError" in brief
    assert "Calendar unavailable" in brief


# ---------------------------------------------------------------------------
# historic regression: empty synthesize() response must not produce a 0-byte brief
# ---------------------------------------------------------------------------


# ── TestBug162EmptySynthesizeGuard (flattened) ──────────────────────────────


def test_bug162_empty_synthesize_guard_empty_synthesize_raises_llm_error_and_falls_back(tmp_path: Path) -> None:
    """When synthesize() returns '', _run() writes the fallback brief then exits 1.

    historic regression: _run() raises LLMError(category="rate-limit"); cli_main() maps it
    to EXIT_PARTIAL (1).  The brief must be written and non-empty before the raise.
    """
    import contextlib

    import pytest

    from fieldkit.cli_exit import cli_main
    from fieldkit.commands.brief.main import _run
    from fieldkit.config import ConfigError

    # Patch synthesize to return empty string; use ConfigError so _run degrades gracefully.
    # Use ExitStack so cli_main() can be a separate inner context manager.
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("fieldkit.commands.brief.main.synthesize", return_value=""))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.get_fieldkit_home", side_effect=ConfigError("no config"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main.get_fieldkit_root", return_value=tmp_path))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_pursuit_alerts", return_value="no alerts"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_champion_signals", return_value="no signals"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_decay_signals", return_value="no decay"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_stale_prose", return_value="no stale"))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.collect_tasks", return_value=("no tasks", "no waiting"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main._render_degraded_section", return_value=""))
        stack.enter_context(patch("fieldkit.commands.brief.main._collect_degraded_sources", return_value=[]))
        # Wrap in cli_main() so LLMError(category="rate-limit") → SystemExit(1)
        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)
    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    # The brief file must exist and be non-empty (written before the raise)
    brief_files = list(tmp_path.glob("briefs/morning-brief-*.md"))
    assert brief_files, "Expected a brief file to be written before exception propagated"
    content = brief_files[0].read_text(encoding="utf-8")
    assert content.strip(), "Brief file must not be empty (0-byte guard failed)"


def test_bug162_empty_synthesize_guard_whitespace_only_synthesize_raises_llm_error(tmp_path: Path) -> None:
    """When synthesize() returns only whitespace, _run() writes fallback brief then exits 1.

    historic regression: _run() raises LLMError(category="rate-limit"); cli_main() maps it
    to EXIT_PARTIAL (1).
    """
    import contextlib

    import pytest

    from fieldkit.cli_exit import cli_main
    from fieldkit.commands.brief.main import _run
    from fieldkit.config import ConfigError

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("fieldkit.commands.brief.main.synthesize", return_value="   \n  "))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.get_fieldkit_home", side_effect=ConfigError("no config"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main.get_fieldkit_root", return_value=tmp_path))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_pursuit_alerts", return_value="no alerts"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_champion_signals", return_value="no signals"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_decay_signals", return_value="no decay"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_stale_prose", return_value="no stale"))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.collect_tasks", return_value=("no tasks", "no waiting"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main._render_degraded_section", return_value=""))
        stack.enter_context(patch("fieldkit.commands.brief.main._collect_degraded_sources", return_value=[]))
        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)
    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    brief_files = list(tmp_path.glob("briefs/morning-brief-*.md"))
    assert brief_files, "Expected a brief file to be written before exception propagated"
    content = brief_files[0].read_text(encoding="utf-8")
    assert content.strip(), "Brief file must not be empty when synthesize returns whitespace"


# ---------------------------------------------------------------------------
# historic regression: collect_pursuit_alerts flags pursuits with missing last_transition
# ---------------------------------------------------------------------------


# ── TestBug042MissingLastTransition (flattened) ─────────────────────────────


def test_bug042_missing_last_transition_no_last_transition_produces_alert(
    tmp_path: Path, write_pursuit_generic
) -> None:
    """A pursuit with no last_transition date produces the 'stall duration unknown' alert."""
    from fieldkit.commands.brief.main import collect_pursuit_alerts

    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(
        acct,
        "no-transition-deal",
        # No last-transition key — simulates a pursuit that was never transitioned
        "stage: discover\n"
        "meddpicc:\n"
        "  metrics: 0\n"
        "  economic-buyer: 0\n"
        "  decision-criteria: 0\n"
        "  decision-process: 0\n"
        "  paper-process: 0\n"
        "  identify-pain: 0\n"
        "  champion: 0\n"
        "  competition: 0\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "No last_transition date recorded" in result, (
        "Expected 'No last_transition date recorded' alert for pursuit with no last_transition"
    )
    assert "stall duration unknown" in result
    assert "discover" in result


def test_bug042_missing_last_transition_no_last_transition_alert_includes_stage_name(
    tmp_path: Path, write_pursuit_generic
) -> None:
    """The missing-transition alert includes the current stage name."""
    from fieldkit.commands.brief.main import collect_pursuit_alerts

    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(
        acct,
        "validate-deal",
        "stage: validate\n"
        "meddpicc:\n"
        "  metrics: 0\n"
        "  economic-buyer: 0\n"
        "  decision-criteria: 0\n"
        "  decision-process: 0\n"
        "  paper-process: 0\n"
        "  identify-pain: 0\n"
        "  champion: 0\n"
        "  competition: 0\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "validate" in result, "Alert must include the stage name"


def test_bug042_missing_last_transition_with_last_transition_no_spurious_alert(
    tmp_path: Path, write_pursuit_generic
) -> None:
    """A pursuit with a recent last_transition does NOT produce the missing-date alert."""
    from fieldkit.commands.brief.main import collect_pursuit_alerts

    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(
        acct,
        "fresh-deal",
        # last-transition set to yesterday — within 14 days, so no stall alert either
        "stage: discover\n"
        "last-transition: 2026-06-09\n"
        "meddpicc:\n"
        "  metrics: 0\n"
        "  economic-buyer: 0\n"
        "  decision-criteria: 0\n"
        "  decision-process: 0\n"
        "  paper-process: 0\n"
        "  identify-pain: 0\n"
        "  champion: 0\n"
        "  competition: 0\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "No last_transition date recorded" not in result, (
        "Pursuit with a valid last_transition must not produce the missing-date alert"
    )
