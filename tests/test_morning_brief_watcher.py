"""Tests for routines/morning_brief_watcher.py — pure-logic paths only.

No MCP calls, no network, no real filesystem access beyond tmp_path.
"""

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------
from fieldkit.watch.morning_brief_collect import (
    _email_domain,
    _parse_internal_domains,
    extract_today_alerts,
    resolve_user_email,
)
from fieldkit.watch.morning_brief_render import _fmt_time, render_brief

pytestmark = pytest.mark.unit

# ===========================================================================
# extract_today_alerts
# ===========================================================================


def _write_alert_file(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "alerts.md"
    p.write_text(content, encoding="utf-8")
    return p


# ── TestExtractTodayAlerts (flattened) ─────────────────────────────────────────────


def test_returns_todays_block(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    content = "## 2026-05-27 — Alert A\n\nSomething happened.\n\n## 2026-05-25 — Old alert\n\nOld stuff.\n"
    p = _write_alert_file(tmp_path, content)
    # lookback_days=0 → today-only; 2026-05-25 is 2 days ago and excluded
    alerts = extract_today_alerts(p, today, lookback_days=0)
    assert len(alerts) == 1
    assert "Alert A" in alerts[0]
    assert "Old alert" not in alerts[0]
    # Contract: must return list[str]
    assert isinstance(alerts, list)
    assert all(isinstance(block, str) for block in alerts)


def test_excludes_old_blocks(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    content = "## 2026-05-01 — Very old\n\nOld body.\n"
    p = _write_alert_file(tmp_path, content)
    assert extract_today_alerts(p, today) == []


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    p = _write_alert_file(tmp_path, "")
    assert extract_today_alerts(p, date.today()) == []


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.md"
    with pytest.raises(FileNotFoundError, match=r"nonexistent\.md"):
        extract_today_alerts(missing, date.today())


def test_deduplicates_same_heading_different_timestamps(tmp_path: Path) -> None:
    """historic regression: duplicate watcher runs produce same heading with different
    'Detected at' timestamps. Only the first block should survive."""
    today = date(2026, 6, 6)
    content = (
        "## 2026-06-06 — acme / ansible-eda — stalled in Qualify\n\n"
        "Detected at: 14:22:57Z\nDays in stage: 45\n\n"
        "## 2026-06-06 — acme / ansible-eda — stalled in Qualify\n\n"
        "Detected at: 19:31:47Z\nDays in stage: 45\n\n"
        "## 2026-06-06 — acme / other-deal — stalled in Validate\n\n"
        "Detected at: 14:22:57Z\nDays in stage: 10\n"
    )
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 2, f"Expected 2 unique blocks, got {len(alerts)}: {alerts}"
    assert "ansible-eda" in alerts[0]
    assert "other-deal" in alerts[1]


def test_multiple_todays_blocks_all_returned(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    content = "## 2026-05-27 — Alert A\n\nBody A.\n\n## 2026-05-27 — Alert B\n\nBody B.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 2
    assert any("Alert A" in a for a in alerts)
    assert any("Alert B" in a for a in alerts)


def test_block_with_no_body_returns_heading_only(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    # Use a date 2 days ago so it is excluded even with lookback_days=1 default
    content = "## 2026-05-27 — Bare heading\n\n## 2026-05-25 — Other\n\nbody\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 1
    assert "Bare heading" in alerts[0]


def test_malformed_date_heading_is_skipped_without_discarding_valid_alerts(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    content = (
        "## 2026-02-30 — Malformed alert\n\nShould be ignored.\n\n## 2026-05-27 — Valid alert\n\nShould be returned.\n"
    )
    p = _write_alert_file(tmp_path, content)

    alerts = extract_today_alerts(p, today, lookback_days=0)

    assert alerts == ["## 2026-05-27 — Valid alert\n\nShould be returned."]


def test_malformed_heading_does_not_crash(tmp_path: Path) -> None:
    today = date(2026, 5, 27)
    # A level-2 heading that doesn't match the date pattern — should be
    # treated as pre-heading text and not cause an IndexError.
    content = "## Not a date heading\n\nsome text\n\n## 2026-05-27 — Good\n\nGood body.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    # Only the date-headed block for today should be returned.
    assert len(alerts) == 1
    assert "Good" in alerts[0]


def test_date_heading_partial_match_excluded(tmp_path: Path) -> None:
    # A heading that starts with today's date but isn't a date heading (edge case).
    today = date(2026, 5, 27)
    content = "## 2026-05-27\n\nbody\n"  # exact date, no extra text
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 1


def test_yesterday_alert_returned_with_default_lookback(tmp_path: Path) -> None:
    """historic regression: default lookback_days=1 includes yesterday's alerts."""
    today = date(2026, 5, 27)
    content = "## 2026-05-27 — Today's alert\n\nToday body.\n\n## 2026-05-26 — Yesterday's alert\n\nYesterday body.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 2
    headings = [a.split("\n")[0] for a in alerts]
    assert any("2026-05-27" in h for h in headings)
    assert any("2026-05-26" in h for h in headings)


def test_two_day_old_alert_excluded_with_default_lookback(tmp_path: Path) -> None:
    """historic regression: alert from 2 days ago is outside the default lookback_days=1 window."""
    today = date(2026, 5, 27)
    content = "## 2026-05-25 — Two days ago\n\nOld body.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert alerts == []


def test_today_alert_still_returned_regression(tmp_path: Path) -> None:
    """Regression: today's alert is always included regardless of lookback setting."""
    today = date(2026, 5, 27)
    content = "## 2026-05-27 — Today only\n\nToday body.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 1
    assert "Today only" in alerts[0]


def test_html_comment_stripped_from_block_body(tmp_path: Path) -> None:
    """historic regression: HTML run-summary comments are removed from alert block bodies."""
    today = date(2026, 5, 27)
    content = "## 2026-05-27 — Alert with comment\n\n<!-- run: 2026-05-27T08:00:00Z -->\nReal alert content here.\n"
    p = _write_alert_file(tmp_path, content)
    alerts = extract_today_alerts(p, today)
    assert len(alerts) == 1
    assert "<!-- run:" not in alerts[0]
    assert "Real alert content here." in alerts[0]


# ===========================================================================
# _email_domain
# ===========================================================================


# ── TestEmailDomain (flattened) ─────────────────────────────────────────────


def test_extracts_domain() -> None:
    assert _email_domain("alice@example.com") == "example.com"  # pii-guard: ignore


def test_lowercases_domain() -> None:
    assert _email_domain("bob@INTERNAL.EXAMPLE.COM") == "internal.example.com"


def test_empty_string_returns_empty() -> None:
    assert _email_domain("") == ""


def test_no_at_sign_returns_empty() -> None:
    assert _email_domain("nodomain") == ""


# pii-guard: ignore


def test_multiple_at_signs_uses_last() -> None:
    # RFC allows only one @, but defensively the impl uses rsplit("@", 1)  # pii-guard: ignore
    result = _email_domain("a@b@c.example.com")
    assert result == "c.example.com"


# pii-guard: ignore

# ===========================================================================  # pii-guard: ignore
# _parse_internal_domains
# ===========================================================================

# pii-guard: ignore
# ── TestGetInternalDomains (flattened) ─────────────────────────────────────────────  # pii-guard: ignore


def test_returns_set() -> None:
    config: dict = {"internal_domains": ["internal.example.com", "ibm.com"]}  # pii-guard: ignore
    domains = _parse_internal_domains(config)
    assert isinstance(domains, set)
    assert "internal.example.com" in domains  # pii-guard: ignore


def test_defaults_when_key_missing() -> None:
    with patch(
        "fieldkit.config.get_internal_domains", return_value=["internal.example.com", "ibm.com"]
    ):  # pii-guard: ignore
        domains = _parse_internal_domains({})
    assert "internal.example.com" in domains  # pii-guard: ignore
    assert "ibm.com" in domains


def test_lowercases_entries() -> None:
    domains = _parse_internal_domains({"internal_domains": ["Internal.Example.Com"]})
    assert "internal.example.com" in domains


# ===========================================================================
# _fmt_time
# ===========================================================================


# ── TestFmtTime (flattened) ─────────────────────────────────────────────


def test_iso_with_z() -> None:
    assert _fmt_time("2026-05-27T09:30:00Z") == "09:30"


def test_iso_with_offset() -> None:
    assert _fmt_time("2026-05-27T14:00:00+05:30") == "14:00"


def test_empty_string() -> None:
    assert _fmt_time("") == ""


def test_unparseable_returns_as_is() -> None:
    assert _fmt_time("not-a-date") == "not-a-date"


# ===========================================================================
# render_brief — source failure labelling
# ===========================================================================


# ── TestRenderBrief (flattened) ─────────────────────────────────────────────

_TARGET_DATE_render_brief = date(2026, 5, 27)


def _patch_fieldkit_home_render_brief(tmp_path: Path) -> None:
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path):
        yield


def _render_render_brief(**overrides) -> str:
    defaults = {
        "target_date": _TARGET_DATE_render_brief,
        "meetings": [],
        "backstory_alerts": [],
        "pursuit_stall_alerts": [],
        "slack_alerts": [],
        "pipeline_review_md": "_[Pipeline review unavailable]_",
        "elapsed_seconds": 0.5,
        "quota_collector": lambda _root: [],
    }
    defaults.update(overrides)
    return render_brief(**defaults)


def test_output_contains_date_heading() -> None:
    output = _render_render_brief()
    assert "# Morning Brief — 2026-05-27" in output


def test_source_failure_backstory() -> None:
    msg = "[Backstory] unavailable: connection refused"
    output = _render_render_brief(backstory_alerts=msg)
    assert "[Backstory] unavailable" in output


def test_source_failure_calendar() -> None:
    # historic regression: calendar error string (with exception type) is surfaced in the brief.
    msg = "_Calendar unavailable (RuntimeError) — check logs for details_"
    output = _render_render_brief(meetings=msg)
    # historic regression: exception type appears in the brief
    assert "RuntimeError" in output
    assert "Calendar unavailable" in output


def test_source_failure_calendar_log_level(caplog: pytest.LogCaptureFixture) -> None:
    """historic regression: calendar exception logged at WARNING; brief shows exception type, not raw message."""
    import logging

    from fieldkit.watch.morning_brief import _collect_calendar_meetings

    with (
        patch("fieldkit.watch.morning_brief.MCPSession") as mock_session_cls,
        caplog.at_level(logging.DEBUG, logger="fieldkit.watch"),
    ):
        # Force the MCP session to raise on initialize so the except branch fires.
        mock_session_cls.return_value.initialize.side_effect = RuntimeError("connection timeout")

        result = _collect_calendar_meetings(
            target_date=date(2026, 5, 27),
            internal_domains=set(),
            user_email="user@example.com",  # pii-guard: ignore
        )

    # historic regression + Constitution VIII: return value shows exception type, NOT raw message.
    assert isinstance(result, str)
    assert "RuntimeError" in result
    assert "Calendar unavailable" in result
    # Raw internal error text must NOT appear (Constitution VIII)
    assert "connection timeout" not in result

    # historic regression: error is logged at WARNING level (was DEBUG before fix).
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING and "fieldkit.watch" in r.name]
    assert any("connection timeout" in r.getMessage() for r in warning_records), (
        "historic regression: full error detail must appear at WARNING level in logs"
    )


def test_source_failure_slack() -> None:
    msg = "[Slack] unavailable: file not found"
    output = _render_render_brief(slack_alerts=msg)
    assert "[Slack] unavailable" in output


def test_source_failure_pursuit_stall() -> None:
    msg = "[Pursuit Stalls] unavailable: parse error"
    output = _render_render_brief(pursuit_stall_alerts=msg)
    assert "[Pursuit Stalls] unavailable" in output


def test_empty_meetings_shows_placeholder() -> None:
    output = _render_render_brief(meetings=[])
    assert "No external meetings" in output


def test_generated_comment_present() -> None:
    output = _render_render_brief()
    assert "<!-- generated:" in output


def test_meeting_entry_rendered() -> None:
    meeting = {
        "title": "Big Deal Sync",
        "start": "2026-05-27T10:00:00Z",
        "end": "2026-05-27T10:30:00Z",
        "attendees": ["alice@customer.example.com"],
        "external_attendees": ["alice@customer.example.com"],
    }
    output = _render_render_brief(meetings=[meeting])
    assert "Big Deal Sync" in output
    assert "alice@customer.example.com" in output


# ===========================================================================
# _collect_pipeline_review
# ===========================================================================


# ── TestCollectPipelineReview (flattened) ─────────────────────────────────────────────


def _patch_fieldkit_home_collect_pipeline_review(tmp_path: Path) -> None:
    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.morning_brief.get_fieldkit_home", return_value=tmp_path),
    ):
        yield


def _render_collect_pipeline_review(**overrides) -> str:
    defaults = {
        "target_date": date(2026, 5, 27),
        "meetings": [],
        "backstory_alerts": [],
        "pursuit_stall_alerts": [],
        "slack_alerts": [],
        "pipeline_review_md": "_[Pipeline review unavailable]_",
        "elapsed_seconds": 0.1,
        "quota_collector": lambda _root: [],
    }
    defaults.update(overrides)
    return render_brief(**defaults)


def test_collect_pipeline_review_returns_pipeline_review_string() -> None:
    """When collect_all_pursuit_data succeeds, returns a non-empty string."""
    from unittest.mock import patch

    from fieldkit.commands.brief.generate import _collect_pipeline_review  # stays in commands layer (tach boundary)

    mock_rows: list = []
    mock_signals: list = []
    mock_blindspots: list = []

    with (
        patch(
            "fieldkit.commands.brief.generate.collect_all_pursuit_data",
            return_value=(mock_rows, mock_signals, mock_blindspots),
        ) as mock_collect,
        patch(
            "fieldkit.commands.brief.generate._render_pipeline_full_brief",
            return_value="# Pipeline Review\n\n_No active pursuits._",
        ) as mock_render,
    ):
        result = _collect_pipeline_review(no_llm=True)

    mock_collect.assert_called_once()
    mock_render.assert_called_once_with(
        mock_rows,
        champion_signals=mock_signals,
        blindspot_data=mock_blindspots,
        no_llm=True,
        today=mock_render.call_args.kwargs["today"],
    )
    assert "Pipeline Review" in result


def test_collect_pipeline_review_dry_run_sets_no_llm_true() -> None:
    """dry_run=True must propagate as no_llm=True to avoid LLM calls."""
    from unittest.mock import patch

    from fieldkit.commands.brief.generate import _collect_pipeline_review  # stays in commands layer (tach boundary)

    with (
        patch(
            "fieldkit.commands.brief.generate.collect_all_pursuit_data",
            return_value=([], [], []),
        ),
        patch(
            "fieldkit.commands.brief.generate._render_pipeline_full_brief",
            return_value="ok",
        ) as mock_render,
    ):
        _collect_pipeline_review(no_llm=True)

    assert mock_render.call_args.kwargs["no_llm"] is True


def test_collect_pipeline_review_returns_error_string_on_failure() -> None:
    """On exception, returns a string containing '[Pipeline Review] unavailable'."""
    from unittest.mock import patch

    from fieldkit.commands.brief.generate import _collect_pipeline_review  # stays in commands layer (tach boundary)

    with patch(
        "fieldkit.commands.brief.generate.collect_all_pursuit_data",
        side_effect=RuntimeError("disk not found"),
    ):
        result = _collect_pipeline_review(no_llm=True)

    assert "[Pipeline Review] unavailable" in result
    assert "disk not found" in result


def test_collect_pipeline_review_pipeline_review_md_included_in_brief() -> None:
    """render_brief includes pipeline_review_md content in output."""
    output = _render_collect_pipeline_review(pipeline_review_md="# Pipeline Review\n\nsome table here")
    assert "some table here" in output


# ===========================================================================
# resolve_user_email
# ===========================================================================


# ── TestResolveUserEmail (flattened) ─────────────────────────────────────────────


def test_prefers_fieldkit_env_var() -> None:
    with patch.dict("os.environ", {"FIELDKIT_USER_EMAIL": "me@example.com"}, clear=False):  # pii-guard: ignore
        result = resolve_user_email({})
    assert result == "me@example.com"  # pii-guard: ignore


def test_returns_empty_when_only_user_set_no_domain_config() -> None:
    # implementation note: hardcoded org-domain fallback removed. Without email_domain  # pii-guard: ignore
    # in config.yaml and no FIELDKIT_USER_EMAIL, resolve_user_email returns "".
    # Patch _load_raw_config so the real config file's email key is not used.
    with (
        patch.dict("os.environ", {"USER": "jdoe"}, clear=True),
        patch("fieldkit.config._loader._load_raw_config", return_value={}),
    ):
        result = resolve_user_email({})
    assert result == ""


def test_returns_empty_when_nothing_set() -> None:
    with patch.dict("os.environ", {}, clear=True):
        result = resolve_user_email({})
    assert result == ""


# ===========================================================================
# _collect_degraded_sources
# ===========================================================================


# ── TestCollectDegradedSources (flattened) ─────────────────────────────────────────────


def test_all_healthy() -> None:
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    sources = {
        "calendar": [{"title": "Standup"}],
        "backstory": ["alert1"],
        "slack": [],
    }
    result = _collect_degraded_sources(sources)
    assert result == []


def test_some_failed() -> None:
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    sources = {
        "calendar": "_unavailable: connection timeout_",
        "backstory": ["alert1"],
        "slack": "_unavailable: file not found_",
    }
    result = _collect_degraded_sources(sources)
    labels = [label for label, _ in result]
    assert "calendar" in labels
    assert "slack" in labels
    assert "backstory" not in labels
    assert len(result) == 2


def test_reason_extracted_from_unavailable_prefix() -> None:
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    sources = {"calendar": "_unavailable: MCP timeout_"}
    result = _collect_degraded_sources(sources)
    assert result == [("calendar", "MCP timeout")]


def test_plain_error_string_used_as_reason() -> None:
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    sources = {"backstory": "some generic error"}
    result = _collect_degraded_sources(sources)
    assert len(result) == 1
    label, reason = result[0]
    assert label == "backstory"
    assert len(reason) > 0


# ===========================================================================
# _render_degraded_section
# ===========================================================================


# ── TestRenderDegradedSection (flattened) ─────────────────────────────────────────────


def test_empty_input_returns_empty_list() -> None:
    from fieldkit.watch.morning_brief_render import _render_degraded_section

    result = _render_degraded_section([])
    assert result == []


def test_with_failures_contains_heading() -> None:
    from fieldkit.watch.morning_brief_render import _render_degraded_section

    degraded = [("calendar", "connection timeout"), ("slack", "file not found")]
    result = _render_degraded_section(degraded)
    text = "\n".join(result)
    assert "## Degraded Sources" in text


def test_with_failures_contains_each_label() -> None:
    from fieldkit.watch.morning_brief_render import _render_degraded_section

    degraded = [("calendar", "connection timeout"), ("slack", "file not found")]
    result = _render_degraded_section(degraded)
    text = "\n".join(result)
    assert "calendar" in text
    assert "slack" in text
    assert "connection timeout" in text
    assert "file not found" in text


# ===========================================================================
# render_brief — degraded section integration
# ===========================================================================


# ── TestRenderBriefDegradedSection (flattened) ─────────────────────────────────────────────

_TARGET_DATE_render_brief_degraded_section = date(2026, 5, 27)


def _patch_fieldkit_home_render_brief_degraded_section(tmp_path: Path) -> None:
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path):
        yield


def _render_render_brief_degraded_section(**overrides) -> str:
    defaults = {
        "target_date": _TARGET_DATE_render_brief_degraded_section,
        "meetings": [],
        "backstory_alerts": [],
        "pursuit_stall_alerts": [],
        "slack_alerts": [],
        "pipeline_review_md": "_[Pipeline review unavailable]_",
        "elapsed_seconds": 0.5,
        "quota_collector": lambda _root: [],
    }
    defaults.update(overrides)
    return render_brief(**defaults)


def test_includes_degraded_section_when_sources_failed() -> None:
    degraded = [("calendar", "MCP timeout"), ("slack", "file not found")]
    output = _render_render_brief_degraded_section(degraded_sources=degraded)
    assert "## Degraded Sources" in output
    assert "calendar" in output
    assert "slack" in output


def test_omits_degraded_section_when_none() -> None:
    output = _render_render_brief_degraded_section(degraded_sources=None)
    assert "## Degraded Sources" not in output


def test_omits_degraded_section_when_empty_list() -> None:
    output = _render_render_brief_degraded_section(degraded_sources=[])
    assert "## Degraded Sources" not in output


# ===========================================================================
# historic regression: ACV formatted as currency in pipeline pulse
# ===========================================================================


# ── TestRenderPipelineSectionAcvFormat (flattened) ─────────────────────────────────────────────


def _make_summary_render_pipeline_section_acv_format(acv: str) -> dict[str, str]:
    return {
        "account": "acme-corp",
        "pursuit": "deal-a",
        "stage": "propose",
        "acv": acv,
        "close_date": "",
        "last_updated": "",
        "next_steps": "",
    }


def test_numeric_acv_formatted_as_currency() -> None:
    from fieldkit.watch.morning_brief_render import _render_pipeline_section

    lines = _render_pipeline_section([_make_summary_render_pipeline_section_acv_format("150000")])
    text = "\n".join(lines)
    assert "$150,000" in text, f"Expected '$150,000' in output, got: {text!r}"


def test_float_acv_formatted_as_currency() -> None:
    from fieldkit.watch.morning_brief_render import _render_pipeline_section

    lines = _render_pipeline_section([_make_summary_render_pipeline_section_acv_format("75000.50")])
    text = "\n".join(lines)
    assert "$75,001" in text or "$75,000" in text, f"Expected formatted ACV in output, got: {text!r}"


def test_non_numeric_acv_passed_through() -> None:
    """Non-numeric ACV (e.g. empty or garbage) is passed through unchanged."""
    from fieldkit.watch.morning_brief_render import _render_pipeline_section

    lines = _render_pipeline_section([_make_summary_render_pipeline_section_acv_format("N/A")])
    text = "\n".join(lines)
    assert "N/A" in text


def test_empty_acv_not_rendered() -> None:
    from fieldkit.watch.morning_brief_render import _render_pipeline_section

    lines = _render_pipeline_section([_make_summary_render_pipeline_section_acv_format("")])
    text = "\n".join(lines)
    assert "ACV" not in text


# ===========================================================================
# historic regression: next_steps "None" string sanitized to empty
# ===========================================================================


# ── TestExtractPursuitSummaryNextSteps (flattened) ─────────────────────────────────────────────


def _write_pursuit_extract_pursuit_summary_next_steps(tmp_path: Path, next_steps_val: str) -> Path:
    account_dir = tmp_path / "acme-corp" / "pursuits"
    account_dir.mkdir(parents=True)
    p = account_dir / "deal.md"
    p.write_text(
        f"---\nstage: propose\nsf_next_steps: {next_steps_val}\n---\n# Deal\n",
        encoding="utf-8",
    )
    return p


def test_none_string_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
        lambda: tmp_path,
    )
    p = _write_pursuit_extract_pursuit_summary_next_steps(tmp_path, "None")
    result = extract_pursuit_summary(p)
    assert result["next_steps"] == "", f"Expected empty string, got: {result['next_steps']!r}"


def test_null_string_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
        lambda: tmp_path,
    )
    p = _write_pursuit_extract_pursuit_summary_next_steps(tmp_path, "null")
    result = extract_pursuit_summary(p)
    assert result["next_steps"] == "", f"Expected empty string, got: {result['next_steps']!r}"


def test_real_next_steps_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.watch.morning_brief_collect import extract_pursuit_summary

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_fieldkit_home",
        lambda: tmp_path,
    )
    p = _write_pursuit_extract_pursuit_summary_next_steps(tmp_path, "Follow up with champion")
    result = extract_pursuit_summary(p)
    assert result["next_steps"] == "Follow up with champion"


# ===========================================================================
# historic regression: RuntimeError message must not embed raw MCP body
# ===========================================================================


# ── TestParseCalendarResultErrorMessage (flattened) ─────────────────────────────────────────────


def test_error_message_is_generic_not_raw_body() -> None:
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    raw_body = "This is a long raw MCP response body with sensitive data " * 30
    try:
        _parse_calendar_result(raw_body)
        raise AssertionError("Expected RuntimeError to be raised")
    except RuntimeError as exc:
        msg = str(exc)
        # Must NOT contain the raw body content
        assert "long raw MCP response body" not in msg, f"Raw body leaked into error message: {msg!r}"
        # Must contain the generic message
        assert "non-JSON text response" in msg or "attendee filtering unavailable" in msg, (
            f"Expected generic error message, got: {msg!r}"
        )


def test_error_message_does_not_include_body_prefix() -> None:
    """The old message started with '[Calendar] returned text summary'; new one must not."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    with pytest.raises(RuntimeError) as exc_info:
        _parse_calendar_result("not json at all")
    assert "text summary" not in str(exc_info.value), f"Old error message pattern still present: {exc_info.value!r}"


# ===========================================================================
# historic regression: cross-account signals scan body lines, not headings
# ===========================================================================


# ── TestDetectCrossAccountSignalsBodyScan (flattened) ─────────────────────────────────────────────


def test_keyword_in_body_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyword that appears only in body text (not headings) is detected.

    historic regression update: each account must have the keyword ≥2 times (min-freq gate).
    The test's intent is to verify body scanning (not headings), so we provide
    two occurrences per account to satisfy the min-freq requirement.
    """
    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    # Create two accounts each with 'kubernetes' in body text only (≥2 times each)
    for slug in ("acme-corp", "globalpay"):
        account_dir = tmp_path / "accounts" / slug
        account_dir.mkdir(parents=True)
        intel = account_dir / "gmail-intel.md"
        intel.write_text(
            "## Email Summary\n\nDiscussed kubernetes migration timeline. kubernetes cluster ready.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-corp", "globalpay"],
    )

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "kubernetes" in topics, f"Expected 'kubernetes' in signals, got: {topics}"


def test_keyword_only_in_heading_not_double_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Headings are excluded; a word only in headings must not appear as a signal."""
    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    # Both accounts have 'openshift' only in a heading, not in body
    for slug in ("acme-corp", "globalpay"):
        account_dir = tmp_path / "accounts" / slug
        account_dir.mkdir(parents=True)
        intel = account_dir / "gmail-intel.md"
        intel.write_text(
            "## OpenShift Migration\n\nNo relevant body content here.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-corp", "globalpay"],
    )

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    # 'openshift' was only in the heading (excluded); 'relevant', 'content',
    # 'here' are in body but are stopwords or short — so no signal expected
    assert "openshift" not in topics, f"'openshift' from heading should not appear in signals: {topics}"


# ===========================================================================
# historic regression: project health section shows when soon > 0 but zombies/expiring == 0
# ===========================================================================


# ── TestRenderProjectHealthSectionSoonCondition (flattened) ─────────────────────────────────────────────


def _make_row_render_project_health_section_soon_condition(health: str) -> Any:
    """Create a minimal mock project row with the given health status."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.health = health
    return row


def test_soon_only_renders_section(tmp_path: Path) -> None:
    """When only SOON projects exist, the section must be rendered (not empty).

    historic regression: the old condition `if zombies == 0 and expiring == 0` caused
    the section to return [] even when soon > 0.  The fix adds `and soon == 0`.
    """
    from unittest.mock import patch as _patch

    from fieldkit.watch.morning_brief_render import _render_project_health_section

    soon_row = _make_row_render_project_health_section_soon_condition("SOON")

    # Build a fake accounts dir with two project files so glob returns paths
    accounts_dir = tmp_path / "accounts"
    proj_file_1 = accounts_dir / "acme-corp" / "projects" / "proj-a.md"
    proj_file_2 = accounts_dir / "globalpay" / "projects" / "proj-b.md"
    proj_file_1.parent.mkdir(parents=True)
    proj_file_2.parent.mkdir(parents=True)
    proj_file_1.write_text("# proj-a\n", encoding="utf-8")
    proj_file_2.write_text("# proj-b\n", encoding="utf-8")

    with (
        _patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        _patch("fieldkit.pursuit.projects.classify_project", return_value=soon_row),
    ):
        result = _render_project_health_section()

    # With 2 SOON rows and 0 zombies/expiring, section must NOT be empty
    assert result != [], "Expected non-empty section when soon > 0"
    text = "\n".join(result)
    assert "SOON" in text, f"Expected 'SOON' in output, got: {text!r}"


def test_all_healthy_returns_empty(tmp_path: Path) -> None:
    """When all projects are HEALTHY (no ZOMBIE/EXPIRING/SOON), returns []."""
    from unittest.mock import patch as _patch

    from fieldkit.watch.morning_brief_render import _render_project_health_section

    healthy_row = _make_row_render_project_health_section_soon_condition("HEALTHY")

    accounts_dir = tmp_path / "accounts"
    proj_file = accounts_dir / "acme-corp" / "projects" / "proj-a.md"
    proj_file.parent.mkdir(parents=True)
    proj_file.write_text("# proj-a\n", encoding="utf-8")

    with (
        _patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        _patch("fieldkit.pursuit.projects.classify_project", return_value=healthy_row),
    ):
        result = _render_project_health_section()

    assert result == [], f"Expected [] for all-healthy projects, got: {result!r}"


# ===========================================================================
# historic regression: pipeline_review only counted as degraded on actual failure
# ===========================================================================


# ── TestBug033PipelineReviewDegradedDetection (flattened) ─────────────────────────────────────────────

_PIPELINE_ERROR_PREFIX_bug033_pipeline_review_degraded_detection = "[Pipeline Review] unavailable:"


def _make_sources_bug033_pipeline_review_degraded_detection(
    pipeline_result: str,
    pipeline_failed: bool,
) -> dict[str, list | str]:
    """Build the all_sources dict as morning_brief.py does after the fix."""
    return {
        "calendar": [],
        "backstory": [],
        "pursuit stalls": [],
        "slack": [],
        "contract expiry": [],
        "draft queue": [],
        "pipeline review": pipeline_result if pipeline_failed else [],
    }


def test_successful_pipeline_review_not_in_degraded() -> None:
    """A successful pipeline review (markdown string) must NOT appear in degraded sources."""
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    success_md = "# Pipeline Review\n\n| Account | Stage |\n|---------|-------|\n| Acme | Discover |\n"
    pipeline_failed = success_md.startswith(_PIPELINE_ERROR_PREFIX_bug033_pipeline_review_degraded_detection)
    sources = _make_sources_bug033_pipeline_review_degraded_detection(success_md, pipeline_failed)

    degraded = _collect_degraded_sources(sources)
    labels = [label for label, _ in degraded]

    assert "pipeline review" not in labels, (
        f"Successful pipeline review must not appear in degraded sources, got: {degraded}"
    )


def test_failed_pipeline_review_is_in_degraded() -> None:
    """A failed pipeline review (error prefix string) must appear in degraded sources."""
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    error_msg = "[Pipeline Review] unavailable: data root not configured"
    pipeline_failed = error_msg.startswith(_PIPELINE_ERROR_PREFIX_bug033_pipeline_review_degraded_detection)
    sources = _make_sources_bug033_pipeline_review_degraded_detection(error_msg, pipeline_failed)

    degraded = _collect_degraded_sources(sources)
    labels = [label for label, _ in degraded]

    assert "pipeline review" in labels, f"Failed pipeline review must appear in degraded sources, got: {degraded}"


def test_write_brief_to_disk_success_not_counted_as_failure(tmp_path: Path) -> None:
    """_write_brief_to_disk must count 0 source_failures when pipeline review succeeded."""
    from unittest.mock import patch as _patch

    from fieldkit.watch.morning_brief import _write_brief_to_disk

    # Simulate the sentinel list [] passed for a successful pipeline review
    sources: list = [[], [], [], [], [], [], []]  # 7 sources, all healthy (lists)

    captured_kwargs: dict = {}

    def _fake_write_run_status(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        _patch("fieldkit.watch.morning_brief.get_watchers_dir", return_value=watchers_dir),
        _patch("fieldkit.watch.morning_brief.write_run_status", side_effect=_fake_write_run_status),
    ):
        exit_code = _write_brief_to_disk(
            "# Brief\n\nContent.\n",
            date(2026, 6, 10),
            1.23,
            sources,
            dry_run=False,
        )

    assert exit_code == 0
    assert captured_kwargs.get("failures") == 0, (
        f"Expected 0 source failures for all-healthy sources, got: {captured_kwargs.get('failures')}"
    )
    assert captured_kwargs.get("outcome") == "ok", f"Expected outcome='ok', got: {captured_kwargs.get('outcome')}"


def test_write_brief_to_disk_error_counted_as_failure(tmp_path: Path) -> None:
    """_write_brief_to_disk must count 1 source_failure when pipeline review failed."""
    from unittest.mock import patch as _patch

    from fieldkit.watch.morning_brief import _write_brief_to_disk

    # Simulate the error string passed for a failed pipeline review
    error_msg = "[Pipeline Review] unavailable: data root not configured"
    sources: list = [[], [], [], [], [], [], error_msg]  # last entry is the error string

    captured_kwargs: dict = {}

    def _fake_write_run_status(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        _patch("fieldkit.watch.morning_brief.get_watchers_dir", return_value=watchers_dir),
        _patch("fieldkit.watch.morning_brief.write_run_status", side_effect=_fake_write_run_status),
    ):
        exit_code = _write_brief_to_disk(
            "# Brief\n\nContent.\n",
            date(2026, 6, 10),
            1.23,
            sources,
            dry_run=False,
        )

    assert exit_code == 0
    assert captured_kwargs.get("failures") == 1, (
        f"Expected 1 source failure for failed pipeline review, got: {captured_kwargs.get('failures')}"
    )
    assert captured_kwargs.get("outcome") == "partial", (
        f"Expected outcome='partial', got: {captured_kwargs.get('outcome')}"
    )


# ===========================================================================
# MCP empty-body guard (spec: mcp-empty-body-guard)
# ===========================================================================


# ── TestMCPSessionPostEmptyBody (flattened) ─────────────────────────────────────────────


# MANUAL-VERIFY: remaining self refs {'self._http'}
def test_empty_body_returns_empty_dicts_without_raising() -> None:
    """_post() returns ({}, {}) when the response body is empty.

    implementation note: MCPSession now uses httpx.Client internally; patching at the
    instance level via monkeypatching self._http.post.
    """
    from unittest.mock import MagicMock

    import httpx

    from fieldkit.watch.morning_brief_mcp import MCPSession

    session = MCPSession(base_url="http://127.0.0.1:8080/v0/groups/fieldkit-calendar/mcp")

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    mock_resp.text = ""  # empty body

    session._http.post = MagicMock(return_value=mock_resp)  # type: ignore[method-assign]

    result = session._post(
        {"jsonrpc": "2.0", "method": "test", "params": {}},
        session_id=None,
    )

    assert result == ({}, {}), f"Expected ({{}}, {{}}) for empty body, got: {result!r}"


# ===========================================================================
# historic regression: CROSS_ACCOUNT_STOPLIST extended + per-account min-frequency gate
# ===========================================================================


# ── TestCrossAccountStoplistExtended (flattened) ─────────────────────────────────────────────


def _make_intel_cross_account_stoplist_extended(tmp_path: Path, slug: str, content: str) -> None:
    account_dir = tmp_path / "accounts" / slug
    account_dir.mkdir(parents=True)
    (account_dir / "gmail-intel.md").write_text(content, encoding="utf-8")


def test_new_stoplist_word_contract_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: 'contract' is in the new stoplist and must not appear as a signal."""
    # Both accounts mention 'contract' multiple times — but it's in the stoplist
    for slug in ("acme-bank", "fixture-domain"):
        _make_intel_cross_account_stoplist_extended(
            tmp_path,
            slug,
            "Discussed contract renewal. The contract terms were reviewed.\n",
        )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-bank", "fixture-domain"],
    )

    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "contract" not in topics, f"'contract' is in stoplist but appeared in signals: {topics}"


def test_new_stoplist_word_enterprise_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: 'enterprise' is in the new stoplist and must not appear as a signal."""
    for slug in ("acme-bank", "fixture-domain"):
        _make_intel_cross_account_stoplist_extended(
            tmp_path,
            slug,
            "Enterprise deployment enterprise architecture discussed.\n",
        )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-bank", "fixture-domain"],
    )

    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "enterprise" not in topics, f"'enterprise' is in stoplist but appeared in signals: {topics}"


def test_new_stoplist_word_business_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: 'business' is in the new stoplist and must not appear as a signal."""
    for slug in ("acme-bank", "fixture-domain"):
        _make_intel_cross_account_stoplist_extended(
            tmp_path,
            slug,
            "Business requirements business case business value.\n",
        )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-bank", "fixture-domain"],
    )

    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "business" not in topics, f"'business' is in stoplist but appeared in signals: {topics}"


# ── TestCrossAccountMinFrequencyGate (flattened) ─────────────────────────────────────────────


def _make_intel_cross_account_min_frequency_gate(tmp_path: Path, slug: str, content: str) -> None:
    account_dir = tmp_path / "accounts" / slug
    account_dir.mkdir(parents=True)
    (account_dir / "gmail-intel.md").write_text(content, encoding="utf-8")


def test_word_once_per_account_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a word appearing once each in two accounts is suppressed (min-freq gate)."""
    # 'kubernetes' appears exactly once in each account — below the ≥2 threshold
    _make_intel_cross_account_min_frequency_gate(tmp_path, "acme-bank", "Discussed kubernetes migration.\n")
    _make_intel_cross_account_min_frequency_gate(tmp_path, "fixture-domain", "Reviewed kubernetes cluster.\n")

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-bank", "fixture-domain"],
    )

    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "kubernetes" not in topics, f"'kubernetes' appeared once per account but was not suppressed: {topics}"


def test_word_twice_in_both_accounts_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a word appearing ≥2 times in each of two accounts surfaces as a signal."""
    # 'kubernetes' appears twice in each account — both meet the ≥2 threshold
    _make_intel_cross_account_min_frequency_gate(
        tmp_path,
        "acme-bank",
        "Discussed kubernetes migration. kubernetes cluster is ready.\n",
    )
    _make_intel_cross_account_min_frequency_gate(
        tmp_path,
        "fixture-domain",
        "Reviewed kubernetes cluster. kubernetes upgrade planned.\n",
    )

    monkeypatch.setattr(
        "fieldkit.watch.morning_brief_collect.get_account_names",
        lambda: ["acme-bank", "fixture-domain"],
    )

    from fieldkit.watch.morning_brief_collect import detect_cross_account_signals

    signals = detect_cross_account_signals(tmp_path)
    topics = [s["topic"] for s in signals]
    assert "kubernetes" in topics, f"'kubernetes' appeared ≥2 times in both accounts but was not surfaced: {topics}"
    kube_signal = next(s for s in signals if s["topic"] == "kubernetes")
    assert "acme-bank" in kube_signal["accounts"]
    assert "fixture-domain" in kube_signal["accounts"]


# ===========================================================================
# historic regression + historic regression: _parse_calendar_result WARNING log level + message
# ===========================================================================


# ── TestParseCalendarResultWarningLog (flattened) ─────────────────────────────────────────────


def test_non_json_logs_warning_with_300_char_sample(caplog: pytest.LogCaptureFixture) -> None:
    """When _parse_calendar_result receives non-JSON, WARNING is logged with ≤300 chars."""
    import logging

    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    # Build a response longer than 300 chars to verify truncation
    long_response = "This is a non-JSON calendar text response. " * 20  # ~860 chars

    with (
        caplog.at_level(logging.WARNING, logger="fieldkit.watch"),
        pytest.raises(RuntimeError, match="non-JSON text response"),
    ):
        _parse_calendar_result(long_response)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING log record"
    log_msg = warning_records[0].getMessage()
    assert "non-JSON response" in log_msg, f"Expected 'non-JSON response' in log: {log_msg!r}"
    # The logged sample must be at most 300 chars (the format string embeds the truncated value)
    # Extract the sample portion after the prefix
    sample_start = log_msg.find("first 300 chars:")
    if sample_start != -1:
        sample = log_msg[sample_start + len("first 300 chars:") :].strip()
        assert len(sample) <= 300, f"Log sample exceeds 300 chars: {len(sample)}"


def test_non_json_raises_runtime_error_with_operator_guidance() -> None:
    """RuntimeError message directs operator to check logs."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    with pytest.raises(RuntimeError, match="check logs"):
        _parse_calendar_result("not valid json")


# ===========================================================================
# historic regression: plain-text calendar parser
# ===========================================================================


# ── TestParseCalendarText (flattened) ─────────────────────────────────────────────


def test_parses_all_day_events() -> None:
    from fieldkit.watch.morning_brief_collect import _parse_calendar_text

    text = (
        "Successfully retrieved 2 events from calendar primary for user@example.com:\n"  # pii-guard: ignore
        ' - "Home" (Starts: 2026-06-10, Ends: 2026-06-11)\n'
        ' - "Team standup" (Starts: 2026-06-11, Ends: 2026-06-11)\n'
    )
    events = _parse_calendar_text(text)
    assert len(events) == 2
    assert events[0]["summary"] == "Home"
    assert events[0]["start"] == {"date": "2026-06-10"}
    assert events[0]["end"] == {"date": "2026-06-11"}
    assert events[0]["attendees"] == []
    assert events[0]["_source"] == "text-format"
    assert events[1]["summary"] == "Team standup"


def test_returns_empty_on_no_match() -> None:
    from fieldkit.watch.morning_brief_collect import _parse_calendar_text

    events = _parse_calendar_text("not valid json at all")
    assert events == []


def test_parse_calendar_result_uses_text_parser_on_non_json() -> None:
    """historic regression: _parse_calendar_result falls through to text parser for plain-text responses."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    text = (
        "Successfully retrieved 1 events from calendar primary for user@example.com:\n"  # pii-guard: ignore
        ' - "All Hands" (Starts: 2026-06-11, Ends: 2026-06-11)\n'
    )
    events = _parse_calendar_result(text)
    assert len(events) == 1
    assert events[0]["summary"] == "All Hands"


def test_parse_calendar_result_treats_no_events_text_as_an_empty_calendar() -> None:
    """An explicit empty calendar is healthy data, not a degraded brief source."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    result = _parse_calendar_result(
        "No events found in calendar 'primary' for user@example.com for the specified time range."
    )

    assert result == []


def test_parse_calendar_result_still_raises_on_unrecognised_text() -> None:
    """_parse_calendar_result raises RuntimeError when text is not JSON and not the known format."""
    from fieldkit.watch.morning_brief_collect import _parse_calendar_result

    with pytest.raises(RuntimeError, match="non-JSON text response"):
        _parse_calendar_result("unrecognised text that is not JSON and not event format")


# ---------------------------------------------------------------------------
# Task 11.6 — get_latest_pursuit_files and _render_project_health_section
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_latest_pursuit_files_returns_sorted_list(tmp_path: Path) -> None:
    """get_latest_pursuit_files returns a list of Path objects sorted by priority."""
    from unittest.mock import patch

    from fieldkit.watch.morning_brief_collect import get_latest_pursuit_files

    # Build a fake accounts dir with two pursuits
    accounts_dir = tmp_path / "accounts"
    acme_pursuits = accounts_dir / "acme-corp" / "pursuits"
    acme_pursuits.mkdir(parents=True)

    # negotiate stage (highest priority = lowest risk index)
    (acme_pursuits / "deal-a.md").write_text(
        "---\nstage: negotiate\nsf_close_date: 2026-07-01\n---\n# Deal A\n",
        encoding="utf-8",
    )
    # qualify stage (lower priority)
    (acme_pursuits / "deal-b.md").write_text(
        "---\nstage: qualify\n---\n# Deal B\n",
        encoding="utf-8",
    )

    with patch("fieldkit.watch.morning_brief_collect._accounts_dir", return_value=accounts_dir):
        result = get_latest_pursuit_files(n=10)

    assert isinstance(result, list)
    assert all(isinstance(p, Path) for p in result)
    # negotiate should sort before qualify
    names = [p.stem for p in result]
    assert "deal-a" in names
    assert "deal-b" in names
    assert names.index("deal-a") < names.index("deal-b")


@pytest.mark.unit
def test_render_project_health_section_returns_empty_on_no_projects(tmp_path: Path) -> None:
    """_render_project_health_section returns [] when no projects/*.md files exist."""
    from unittest.mock import patch

    from fieldkit.watch.morning_brief_render import _render_project_health_section

    # Patch get_fieldkit_home to point to tmp_path (no accounts/*/projects/ dirs)
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path):
        result = _render_project_health_section()

    assert result == []


# ---------------------------------------------------------------------------
# Task 11.7 — MCPSession.call_tool raises RuntimeError when not initialized
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_call_tool_raises_when_not_initialized() -> None:
    """MCPSession.call_tool raises RuntimeError when initialize() has not been called."""
    from fieldkit.watch.morning_brief_mcp import MCPSession

    session = MCPSession("http://127.0.0.1:9999/mcp")
    # No initialize() call — _session_id is None
    with pytest.raises(RuntimeError, match="initialize"):
        session.call_tool("some_tool", {})


# ===========================================================================
# implementation note: fetch_external_meetings includes events with no attendee metadata
# ===========================================================================


# ── TestFetchExternalMeetingsAttendeeFilter (flattened) ─────────────────────────────────────────────


def _make_session_fetch_external_meetings_attendee_filter(events: list[dict[str, Any]]) -> Any:
    """Return a mock MCPSession whose call_tool returns *events*."""
    from unittest.mock import MagicMock

    session = MagicMock()
    session.call_tool.return_value = events
    return session


def test_event_with_no_attendees_but_summary_included() -> None:
    """implementation note: event with empty attendees list and non-empty summary appears in result."""
    from fieldkit.watch.morning_brief_collect import fetch_external_meetings

    event = {
        "summary": "Customer sync",
        "attendees": [],
        "start": {"dateTime": "2026-06-24T10:00:00Z"},
        "end": {"dateTime": "2026-06-24T10:30:00Z"},
    }
    session = _make_session_fetch_external_meetings_attendee_filter([event])
    result = fetch_external_meetings(
        session,
        target_date=date(2026, 6, 24),
        internal_domains={"example.com"},
        user_email="user@example.com",  # pii-guard: ignore
    )
    assert len(result) == 1, f"Expected 1 meeting, got {len(result)}: {result}"
    assert result[0]["title"] == "Customer sync"


def test_event_with_no_attendees_and_empty_summary_skipped() -> None:
    """implementation note: event with empty attendees and empty summary is skipped."""
    from fieldkit.watch.morning_brief_collect import fetch_external_meetings

    event = {
        "summary": "",
        "attendees": [],
        "start": {"dateTime": "2026-06-24T10:00:00Z"},
        "end": {"dateTime": "2026-06-24T10:30:00Z"},
    }
    session = _make_session_fetch_external_meetings_attendee_filter([event])
    result = fetch_external_meetings(
        session,
        target_date=date(2026, 6, 24),
        internal_domains={"example.com"},
        user_email="user@example.com",  # pii-guard: ignore
    )
    assert result == [], f"Expected no meetings, got: {result}"


def test_event_with_no_attendees_and_whitespace_summary_skipped() -> None:
    """implementation note: event with empty attendees and whitespace-only summary is skipped."""
    from fieldkit.watch.morning_brief_collect import fetch_external_meetings

    event = {
        "summary": "   ",
        "attendees": [],
        "start": {"dateTime": "2026-06-24T10:00:00Z"},
        "end": {"dateTime": "2026-06-24T10:30:00Z"},
    }
    session = _make_session_fetch_external_meetings_attendee_filter([event])
    result = fetch_external_meetings(
        session,
        target_date=date(2026, 6, 24),
        internal_domains={"example.com"},
        user_email="user@example.com",  # pii-guard: ignore
    )
    assert result == [], f"Expected no meetings for whitespace summary, got: {result}"


def test_event_with_external_attendee_still_included() -> None:
    """Regression: events with external attendees continue to be included."""
    from fieldkit.watch.morning_brief_collect import fetch_external_meetings

    event = {
        "summary": "Deal review",
        "attendees": [{"email": "buyer@acme-corp.com"}],
        "start": {"dateTime": "2026-06-24T14:00:00Z"},
        "end": {"dateTime": "2026-06-24T14:30:00Z"},
    }
    session = _make_session_fetch_external_meetings_attendee_filter([event])
    result = fetch_external_meetings(
        session,
        target_date=date(2026, 6, 24),
        internal_domains={"example.com"},
        user_email="user@example.com",  # pii-guard: ignore
    )
    assert len(result) == 1
    assert result[0]["title"] == "Deal review"
    assert "buyer@acme-corp.com" in result[0]["external_attendees"]


# ===========================================================================
# implementation note: SourceNotReady sentinel — optional watchers don't inflate source_failures
# ===========================================================================


# ── TestSourceNotReady (flattened) ─────────────────────────────────────────────


def test_source_not_ready_is_not_a_str() -> None:
    """SourceNotReady must NOT be a str subclass — isinstance(x, str) must be False."""
    from fieldkit.watch._morning_brief_types import SourceNotReady

    snr = SourceNotReady("_No data yet._")
    assert not isinstance(snr, str), "SourceNotReady must not be a str subclass"


def test_source_not_ready_not_counted_in_source_failures(tmp_path: Path) -> None:
    """implementation note: _write_brief_to_disk counts 0 failures when optional sources return SourceNotReady."""
    from unittest.mock import patch as _patch

    from fieldkit.watch._morning_brief_types import SourceNotReady
    from fieldkit.watch.morning_brief import _write_brief_to_disk

    # Simulate: calendar healthy (list), backstory healthy (list),
    # pursuit stalls healthy (list), slack/contract/draft not ready (SourceNotReady),
    # pipeline review healthy (empty list sentinel).
    sources: list = [
        [],  # calendar
        [],  # backstory
        [],  # pursuit stalls
        SourceNotReady("_No Slack thread data yet._"),
        SourceNotReady("_No contract expiry data yet._"),
        SourceNotReady("_No draft queue data yet._"),
        [],  # pipeline review (healthy sentinel)
    ]

    captured_kwargs: dict = {}

    def _fake_write_run_status(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    with (
        _patch("fieldkit.watch.morning_brief.get_watchers_dir", return_value=watchers_dir),
        _patch("fieldkit.watch.morning_brief.write_run_status", side_effect=_fake_write_run_status),
    ):
        exit_code = _write_brief_to_disk(
            "# Brief\n\nContent.\n",
            date(2026, 6, 24),
            0.5,
            sources,
            dry_run=False,
        )

    assert exit_code == 0
    assert captured_kwargs.get("failures") == 0, (
        f"implementation note: SourceNotReady must not inflate source_failures, got: {captured_kwargs.get('failures')}"
    )
    assert captured_kwargs.get("outcome") == "ok", (
        f"implementation note: outcome must be 'ok' when only optional sources are not ready, "
        f"got: {captured_kwargs.get('outcome')!r}"
    )


def test_source_not_ready_excluded_from_degraded_sources() -> None:
    """implementation note: _collect_degraded_sources must not list SourceNotReady as degraded."""
    from fieldkit.watch._morning_brief_types import SourceNotReady
    from fieldkit.watch.morning_brief_render import _collect_degraded_sources

    sources = {
        "calendar": [],
        "backstory": [],
        "slack": SourceNotReady("_No Slack thread data yet._"),
        "contract expiry": SourceNotReady("_No contract expiry data yet._"),
        "draft queue": SourceNotReady("_No draft queue data yet._"),
    }
    degraded = _collect_degraded_sources(sources)
    labels = [label for label, _ in degraded]

    assert "slack" not in labels, f"implementation note: SourceNotReady 'slack' must not appear in degraded: {degraded}"
    assert "contract expiry" not in labels, (
        f"implementation note: SourceNotReady 'contract expiry' must not appear in degraded: {degraded}"
    )
    assert "draft queue" not in labels, (
        f"implementation note: SourceNotReady 'draft queue' must not appear in degraded: {degraded}"
    )
    assert degraded == [], f"implementation note: expected no degraded sources, got: {degraded}"


def test_source_not_ready_rendered_as_message() -> None:
    """implementation note: SourceNotReady.message is rendered as section content (same display as before)."""
    from fieldkit.watch._morning_brief_types import SourceNotReady
    from fieldkit.watch.morning_brief_render import _render_alert_blocks

    snr = SourceNotReady("_No Slack thread data yet — run `fieldkit watch slack-threads` first._")
    result = _render_alert_blocks(snr, "_No alerts today._")

    assert result == [snr.message], f"Expected [message], got: {result!r}"
    assert "slack-threads" in result[0]


def test_collect_alert_source_returns_source_not_ready_for_missing_optional(tmp_path: Path) -> None:
    """implementation note: _collect_alert_source returns SourceNotReady (not str) for missing optional files."""
    from fieldkit.watch._morning_brief_types import SourceNotReady
    from fieldkit.watch.morning_brief import _collect_alert_source

    missing_file = tmp_path / "slack-thread-alerts.md"
    not_found_msg = "_No Slack thread data yet._"

    result = _collect_alert_source(
        missing_file,
        date(2026, 6, 24),
        "Slack",
        not_found_msg=not_found_msg,
    )

    assert isinstance(result, SourceNotReady), (
        f"implementation note: expected SourceNotReady, got {type(result).__name__}: {result!r}"
    )
    assert result.message == not_found_msg


def test_fresh_install_no_optional_files_outcome_is_ok(tmp_path: Path) -> None:
    """implementation note: fresh install with no optional watcher files → outcome='ok', failures=0.

    Simulates the scenario where slack-threads, contract-expiry, and draft-queue
    watchers have never been run (no alert files exist).
    """
    from unittest.mock import patch as _patch

    from fieldkit.watch._morning_brief_types import SourceNotReady
    from fieldkit.watch.morning_brief import _collect_alert_source, _write_brief_to_disk

    target_date = date(2026, 6, 24)
    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()

    # Collect optional sources — all files absent, all return SourceNotReady
    slack_result = _collect_alert_source(
        watchers_dir / "slack-thread-alerts.md",
        target_date,
        "Slack",
        not_found_msg="_No Slack thread data yet._",
    )
    contract_result = _collect_alert_source(
        watchers_dir / "contract-expiry-alerts.md",
        target_date,
        "Contract Expiry",
        not_found_msg="_No contract expiry data yet._",
    )
    draft_result = _collect_alert_source(
        watchers_dir / "draft-queue-alerts.md",
        target_date,
        "Draft Queue",
        not_found_msg="_No draft queue data yet._",
    )

    # All three must be SourceNotReady
    assert isinstance(slack_result, SourceNotReady)
    assert isinstance(contract_result, SourceNotReady)
    assert isinstance(draft_result, SourceNotReady)

    # Simulate _write_brief_to_disk with all healthy + 3 SourceNotReady optional sources
    sources: list = [
        [],  # calendar (healthy)
        [],  # backstory (healthy)
        [],  # pursuit stalls (healthy)
        slack_result,
        contract_result,
        draft_result,
        [],  # pipeline review (healthy sentinel)
    ]

    captured_kwargs: dict = {}

    def _fake_write_run_status(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    with (
        _patch("fieldkit.watch.morning_brief.get_watchers_dir", return_value=watchers_dir),
        _patch("fieldkit.watch.morning_brief.write_run_status", side_effect=_fake_write_run_status),
    ):
        exit_code = _write_brief_to_disk(
            "# Brief\n\nContent.\n",
            target_date,
            0.3,
            sources,
            dry_run=False,
        )

    assert exit_code == 0
    assert captured_kwargs.get("failures") == 0, (
        f"implementation note: fresh install must have 0 source_failures, got: {captured_kwargs.get('failures')}"
    )
    assert captured_kwargs.get("outcome") == "ok", (
        f"implementation note: fresh install outcome must be 'ok', got: {captured_kwargs.get('outcome')!r}"
    )
