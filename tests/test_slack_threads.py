"""Unit tests for fieldkit/commands/watch/slack_threads.py.

Focus: _extract_message_text cascade fallback logic (implementation note).
"""

import pytest

from fieldkit.watch.slack_thread_classification import _extract_message_text

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _extract_message_text — cascade fallback (implementation note)
# ---------------------------------------------------------------------------


def test_extract_message_text_primary_text() -> None:
    """Level 1: top-level text field is returned when present and non-empty."""
    msg = {"text": "direct text", "attachments": [{"text": "attachment text"}]}
    assert _extract_message_text(msg) == "direct text"


def test_extract_message_text_empty_text_falls_to_attachment() -> None:
    """Level 2: empty top-level text falls through to attachment."""
    msg = {"text": "", "attachments": [{"text": "fallback"}]}
    assert _extract_message_text(msg) == "fallback"


def test_extract_message_text_absent_text_falls_to_attachment() -> None:
    """Level 2: absent top-level text falls through to attachment."""
    msg = {"attachments": [{"text": "hello"}]}
    assert _extract_message_text(msg) == "hello"


def test_extract_message_text_rich_text_block_fallback() -> None:
    """Level 3: rich_text block traversal when text and attachments are absent."""
    msg = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "block text"}],
                    }
                ],
            }
        ]
    }
    assert _extract_message_text(msg) == "block text"


def test_extract_message_text_all_empty_returns_empty_string() -> None:
    """All sources empty → empty string, no exception."""
    assert _extract_message_text({}) == ""
    assert _extract_message_text({"text": "", "attachments": [], "blocks": []}) == ""


def test_extract_message_text_attachment_not_a_dict_safe() -> None:
    """Level 2: attachments[0] is not a dict → skipped, no AttributeError."""
    msg = {"attachments": ["not-a-dict"]}
    assert _extract_message_text(msg) == ""


def test_extract_message_text_empty_blocks_list_safe() -> None:
    """Level 3: empty blocks list → returns '' without IndexError."""
    msg = {"blocks": []}
    assert _extract_message_text(msg) == ""


def test_extract_message_text_non_standard_rich_text_block_safe() -> None:
    """Level 3: rich_text block with unexpected shape → skipped, no KeyError."""
    # block["text"] is a string instead of a dict (non-standard Slack response)
    msg = {"blocks": [{"type": "rich_text", "text": "not-a-dict"}]}
    assert _extract_message_text(msg) == ""


def test_extract_message_text_skips_non_rich_text_blocks() -> None:
    """Level 3: only rich_text blocks are inspected; section blocks are skipped."""
    msg = {
        "blocks": [
            # section block — has block["text"]["text"] but wrong type
            {"type": "section", "text": {"type": "mrkdwn", "text": "section text"}},
            # rich_text block — should be found
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "rich"}]}],
            },
        ]
    }
    assert _extract_message_text(msg) == "rich"


def test_extract_message_text_whitespace_only_text_falls_through() -> None:
    """Whitespace-only top-level text is treated as empty and falls through."""
    msg = {"text": "   ", "attachments": [{"text": "attachment"}]}
    assert _extract_message_text(msg) == "attachment"
