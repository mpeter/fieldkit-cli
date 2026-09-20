"""Companion outbox pointer coverage for morning-brief rendering."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_render import _render_companion_outbox_pointer, render_brief

pytestmark = pytest.mark.unit


def test_companion_pointer_missing_outbox_is_empty(tmp_path: Path) -> None:
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_data", return_value=tmp_path):
        result = _render_companion_outbox_pointer()

    assert result == []


def test_companion_pointer_empty_outbox_is_empty(tmp_path: Path) -> None:
    (tmp_path / "companion-outbox").mkdir(mode=0o700)
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_data", return_value=tmp_path):
        result = _render_companion_outbox_pointer()

    assert result == []


def test_companion_pointer_unreadable_outbox_is_empty(tmp_path: Path) -> None:
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.watch.morning_brief_render.list_proposals", side_effect=OSError("unreadable")),
    ):
        result = _render_companion_outbox_pointer()

    assert result == []


def test_companion_pointer_populated_outbox_has_exact_output(tmp_path: Path) -> None:
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    (outbox / "proposal.md").write_text("proposal", encoding="utf-8")
    (outbox / "proposal.proposal.json").write_text(
        '{"command_argv":null,"created_at":"2026-09-10T12:00:00+00:00",'
        '"item_id":"0123456789abcdef","markdown":"# Current\\n","version":1}',
        encoding="utf-8",
    )
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_data", return_value=tmp_path):
        result = _render_companion_outbox_pointer()

    assert result == [f"**2 companion proposal(s) pending review** — {outbox}", ""]


def test_render_brief_places_companion_pointer_before_footer(tmp_path: Path) -> None:
    outbox = tmp_path / "companion-outbox"
    outbox.mkdir(mode=0o700)
    (outbox / "proposal.md").write_text("proposal", encoding="utf-8")
    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.watch.morning_brief_render.get_pipeline_quota", return_value=None),
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
    ):
        rendered = render_brief(
            target_date=date(2026, 9, 9),
            meetings=[],
            backstory_alerts=[],
            pursuit_stall_alerts=[],
            slack_alerts=[],
            pipeline_review_md="",
            elapsed_seconds=0.0,
            quota_collector=lambda _root: [],
        )

    pointer = f"**1 companion proposal(s) pending review** — {outbox}"
    assert f"{pointer}\n\n\n---\n\n<!-- generated:" in rendered
