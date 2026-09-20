"""Dashboard shell contracts for companion controls."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_STATIC = Path("src/fieldkit/web/static")


def test_feed_and_outbox_are_top_level_tabs() -> None:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="feed"' in html
    assert 'data-tab="outbox"' in html
    assert 'id="task-create-form"' in html


def test_ui_escapes_feed_and_outbox_fields_and_refreshes_all_views() -> None:
    script = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert 'esc(String(item.summary || ""))' in script
    assert "proposal.validation_error" in script
    assert "Review only" in script
    assert "Promise.all([loadTasks(), loadFeed(), loadOutbox(), loadOperations()])" in script


def test_write_controls_require_companion_status() -> None:
    script = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert 'api("/api/companion")' in script
    assert "!companion.writes_enabled" in script
    assert 'method: "POST"' in script
    assert "body.error || body.message" in script


def test_service_worker_cache_is_v3() -> None:
    script = (_STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'const CACHE = "fieldkit-v3"' in script


def test_chat_history_payload_bounds_each_turn_before_submission() -> None:
    script = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "const MAX_CHAT_HISTORY_TURN_CHARS = 2000" in script
    assert "content: turn.content.slice(0, MAX_CHAT_HISTORY_TURN_CHARS)" in script
    assert "history: chatHistoryPayload()" in script
