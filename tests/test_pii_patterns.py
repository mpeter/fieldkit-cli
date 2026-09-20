"""Tests for scripts/pii_patterns.py — redaction engine + notice-safe descriptions.

The change descriptions returned by ``redact_text`` are posted verbatim into
public GitHub notice comments (see scripts/check_pii_payload.py). The critical
invariant is that a description must NEVER echo the original PII value it just
scrubbed — otherwise the "auto-redact" comment re-leaks the data.

Real account slugs are pulled from private runtime sources rather than
written as literals, so no real customer name appears in this source file.
Email / home-path / Slack inputs are invented, not real PII. The file is still in
pii_guard._SKIP_FILES so the synthetic PII-shaped inputs don't trip the hook.
"""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pii_patterns  # noqa: E402
from pii_patterns import _FIXTURE_ACCOUNT_SLUGS, redact_text, summarize_changes  # noqa: E402

pytestmark = pytest.mark.unit

# A synthetic stand-in for "a real customer slug" — never a real customer name
# and deliberately NOT one of the approved fixtures. The redaction pattern is
# compiled at import time, so tests patch the module global to recognize it.
_A_REAL_SLUG = "realcustomer-inc"
_A_REAL_SLUG_RE = re.compile(rf"\b({_A_REAL_SLUG})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Notice-safety: descriptions never echo the original PII value
# ---------------------------------------------------------------------------


def test_account_slug_description_does_not_echo_original_value() -> None:
    body = f"The {_A_REAL_SLUG} migration is blocked."
    with patch.object(pii_patterns, "_KNOWN_ACCOUNT_SLUGS_RE", _A_REAL_SLUG_RE):
        redacted, descs = redact_text(body)

    assert _A_REAL_SLUG not in redacted.lower()
    assert descs == ["account name → `<account-slug>`"]
    # The exact regression this guards: the original slug must not appear in
    # any description (case-insensitively).
    assert all(_A_REAL_SLUG not in d.lower() for d in descs)


@pytest.mark.parametrize("slug", sorted(_FIXTURE_ACCOUNT_SLUGS))
def test_approved_fixture_slugs_are_not_redacted(slug: str) -> None:
    """Fake fixtures are safe to appear in public issue/PR bodies (R23)."""
    body = f"The {slug} example walkthrough is ready."
    redacted, descs = redact_text(body)

    assert redacted == body
    assert descs == []


def test_email_description_does_not_echo_original_value() -> None:
    email = "realperson@" + "realcorp.invalid"
    body = f"ping {email} about it"
    redacted, descs = redact_text(body)

    assert email not in redacted
    assert descs == ["personal email → `<redacted-email>`"]
    assert all("realperson" not in d for d in descs)  # pii-guard: ignore


def test_home_path_description_does_not_echo_original_value() -> None:
    body = "see /home/someperson/notes.txt"  # pii-guard: ignore
    redacted, descs = redact_text(body)

    assert "someperson" not in redacted  # pii-guard: ignore
    assert descs == ["absolute home path → `<user-home-path>`"]
    assert all("someperson" not in d for d in descs)  # pii-guard: ignore


def test_slack_username_description_does_not_echo_original_value() -> None:
    body = "raised by from:realhandle in thread"  # pii-guard: ignore
    redacted, descs = redact_text(body)

    assert "realhandle" not in redacted  # pii-guard: ignore
    assert descs == ["Slack username → `from:your-username`"]
    assert all("realhandle" not in d for d in descs)  # pii-guard: ignore


def test_clean_body_produces_no_changes() -> None:
    body = "Nothing sensitive here — see example.com for details."
    redacted, descs = redact_text(body)

    assert redacted == body
    assert descs == []


# ---------------------------------------------------------------------------
# summarize_changes: collapse per-occurrence entries into counted lines
# ---------------------------------------------------------------------------


def test_summarize_changes_collapses_duplicates_with_count() -> None:
    changes = [
        "account name → `<account-slug>`",
        "account name → `<account-slug>`",
        "personal email → `<redacted-email>`",
    ]

    summary = summarize_changes(changes)

    assert summary == [
        "account name → `<account-slug>` (x2)",
        "personal email → `<redacted-email>`",
    ]


def test_summarize_changes_preserves_first_appearance_order() -> None:
    changes = ["b", "a", "b", "a", "a"]

    summary = summarize_changes(changes)

    assert summary == ["b (x2)", "a (x3)"]


def test_summarize_changes_empty_returns_empty() -> None:
    assert summarize_changes([]) == []
