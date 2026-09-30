"""Semantic contract for the public Slack digest skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILL = Path(__file__).parents[1] / "src/fieldkit/skills/slack-digest/SKILL.md"


def test_slack_digest_describes_the_actual_bounded_watcher() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    for required in (
        "first configured account keyword",
        "limits returned messages",
        "filters by configured account-channel names",
        "excludes internal-channel patterns",
        "Accounts marked `internal` or `slack_watch: false` are skipped",
    ):
        assert required in one_line
    assert "not a complete deal-room digest" in content


def test_slack_digest_keeps_auth_and_incomplete_reads_non_passing() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "Authentication failures exit 2" in one_line
    assert "provider or persistence failures exit 1" in one_line
    assert "invalid arguments or configuration exit 3" in one_line
    assert "Those results remain non-passing" in content
    assert "A failure or empty check does not prove no account activity" in one_line
    assert "Do not assume tokens are in `.env`" in content
    assert "An operator-approved cache" in content
    assert "known age, coverage, and provenance" in one_line


def test_slack_digest_does_not_hide_reads_writes_or_identity_gaps() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "A preview still makes live Slack reads" in one_line
    assert "does not create or prune fieldkit watcher logs, alerts, state, or run status" in one_line
    assert "the configured external client's own effects are separate" in one_line
    assert "never send, draft, or post Slack messages" in one_line
    assert "[Unresolved: U0XXXXXX]" in content
    assert "confined atomic write, and read-back" in one_line
    assert "```" not in content
    assert "|---" not in content
