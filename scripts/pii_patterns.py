#!/usr/bin/env python3
"""PII detection and redaction patterns — shared engine.

Pattern source: hooks/pii_guard.py (kept in sync manually).
Detection logic is identical to the pre-commit hook; this module adds
line-level redaction on top of detection so that GitHub issue/PR bodies
can be scrubbed in-place.

Public API
----------
redact_text(text)  -> (redacted_str, list[str])
    Apply all PII redactions to a multi-line string.
    Returns the cleaned text and a human-readable list of what changed.

has_pii(text)  -> bool
    Return True if text contains any PII patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Approved synthetic fixture slugs — fake stand-in accounts, never redacted.
# Keep in sync with hooks/pii_guard.py:_FIXTURE_ACCOUNT_SLUGS.
# ---------------------------------------------------------------------------
_FIXTURE_ACCOUNT_SLUGS: frozenset[str] = frozenset(
    [
        "acme-corp",  # pii-guard: ignore — approved synthetic fixture slug
        "acme-bank",  # pii-guard: ignore — approved synthetic fixture slug
        "midwest-ins",  # pii-guard: ignore — approved synthetic fixture slug
        "globalpay",  # pii-guard: ignore — approved synthetic fixture slug
    ]
)

# ---------------------------------------------------------------------------
# Seed slugs — real customer account slugs to redact even without live config.
# Keep in sync with hooks/pii_guard.py:_SEED_ACCOUNT_SLUGS.
# ---------------------------------------------------------------------------
_SEED_ACCOUNT_SLUGS: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Detection patterns (mirrors hooks/pii_guard.py exactly)
# ---------------------------------------------------------------------------

_ABS_HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z][A-Za-z0-9_.-]{1,30}/")  # pii-guard: ignore

_SLACK_FROM_RE = re.compile(r'from:[a-z][a-z0-9_.-]{2,30}(?:"|\s|$)', re.IGNORECASE)

_PERSONAL_EMAIL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9._%+-]{2,30})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# ---------------------------------------------------------------------------
# Allowlists (mirrors hooks/pii_guard.py exactly)
# ---------------------------------------------------------------------------

_SAFE_EMAIL_LOCAL = re.compile(
    r"""(?xi)
    ^(
        YOUR[_-]?EMAIL | your[_-]?email | your[_-]?name |
        user | name | example | test | noreply | no-reply |
        jane | bob | alice | carol | joe | john | admin |
        placeholder | email | sender | recipient | contact |
        someone | anyone | notifications | info | hello |
        colleague | support
    )$
    """,
    re.IGNORECASE,
)

_SAFE_EMAIL_DOMAIN = re.compile(
    r"""(?xi)
    ^(
        example\.com | example\.org | example\.net |
        globalpay\.com | midwestins\.com | acme-corp\.com | acme\.com |
        acme-bank\.com | acmebank\.com | fixture-domain\.com |
        client\.com | company\.com |
        x\.com | r\.com |
        your-org\.com | your-company\.com | yourcompany\.com |
        gmail\.com
    )$
    """,
    re.IGNORECASE,
)

_SAFE_LINE_MARKERS = [
    "user_google_email",
    "# pii-guard: ignore",
    "<account-slug>",
    "<Account Name>",
    "YOUR_EMAIL",
    "your-email",
    "<email>",
    "example.com",
    "your-org.com",
    "placeholder",
    "mailto:email@domain",
    "gemini-notes@google.com",
    "notifications@github-com.example.com",
    "@your-org.com",
    "user@internal.example.com",
    "jane@globalpay-com.example.com",
    "jane@",
    "bob@",
    "alice@",
    "jdoe@",
    "jae@",
    "t@r-com.example.com",
    "j@r-com.example.com",
    "bounce+",
    "RFC Message-ID",
    "<msg@gmail-com.example.com>",
    "colleague@your-org-com.example.com",
    "contact@globalpay-com.example.com",
    "contact2@globalpay-com.example.com",
    "contact3@globalpay-com.example.com",
    "{{",
]


def _build_account_slug_pattern() -> re.Pattern[str]:
    """Build a regex matching any known real customer account slug.

    Attempts to load live account slugs from the fieldkit config.
    Falls back to seed-only set on any failure (CI, missing config, etc.).
    """
    slugs: set[str] = set(_SEED_ACCOUNT_SLUGS)
    try:
        import sys as _sys

        _repo_root = Path(__file__).resolve().parents[1]
        if str(_repo_root) not in _sys.path:
            _sys.path.insert(0, str(_repo_root / "src"))
        from fieldkit.config import get_account_names  # pii-guard: ignore

        for name in get_account_names():
            if name and isinstance(name, str):
                slugs.add(name.lower())
    except Exception:  # noqa: BLE001
        pass

    # Approved synthetic fixtures are never redacted, even if accounts.yaml
    # lists one. Subtract after the union so no source can re-add a fixture.
    slugs -= _FIXTURE_ACCOUNT_SLUGS

    if not slugs:
        # No real slugs to redact; return a pattern that never matches (an empty
        # alternation r"\b()\b" would match at every word boundary).
        return re.compile(r"(?!)")

    alternation = "|".join(re.escape(s) for s in sorted(slugs))
    return re.compile(rf"\b({alternation})\b", re.IGNORECASE)


_KNOWN_ACCOUNT_SLUGS_RE: re.Pattern[str] = _build_account_slug_pattern()  # pii-guard: ignore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_safe_email(local: str, domain: str) -> bool:
    if _SAFE_EMAIL_LOCAL.match(local):
        return True
    return bool(_SAFE_EMAIL_DOMAIN.match(domain))


def _is_safe_line(line: str) -> bool:
    low = line.lower()
    return any(m.lower() in low for m in _SAFE_LINE_MARKERS)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact_line(line: str) -> tuple[str, list[str]]:
    """Return (redacted_line, list_of_change_descriptions) for one line.

    If the line matches any safe marker the original line is returned unchanged.
    """
    if _is_safe_line(line):
        return line, []

    changes: list[str] = []
    result = line

    # Change descriptions name the *category* redacted, never the original
    # value — these strings are posted verbatim into public GitHub notice
    # comments, so echoing the matched PII would re-leak what we just scrubbed.

    # --- absolute home paths ---
    # Replace only the home-prefix segment; preserve everything after the trailing /
    def _replace_home(m: re.Match[str]) -> str:
        # m.group(0) is e.g. "/home/<user>/"  # pii-guard: ignore — example in comment
        # We replace it with "<user-home-path>/" preserving the slash so
        # that any path continuation remains valid.
        changes.append("absolute home path → `<user-home-path>`")
        return "<user-home-path>/"

    result = _ABS_HOME_RE.sub(_replace_home, result)

    # --- personal email addresses ---
    def _replace_email(m: re.Match[str]) -> str:
        local, domain = m.group(1), m.group(2)
        if _is_safe_email(local, domain):
            return m.group(0)
        changes.append("personal email → `<redacted-email>`")
        return "<redacted-email>"

    result = _PERSONAL_EMAIL_RE.sub(_replace_email, result)

    # --- Slack from:<username> ---
    def _replace_slack(m: re.Match[str]) -> str:
        raw = m.group(0)
        token = raw.split(":")[1].rstrip("\"' ").lower()
        if token in ("your-username", "user", "username", "you"):
            return raw
        # Preserve any trailing delimiter (space, quote, EOL) after the token
        suffix = raw[len("from:") + len(token) :]
        changes.append("Slack username → `from:your-username`")
        return f"from:your-username{suffix}"

    result = _SLACK_FROM_RE.sub(_replace_slack, result)

    # --- account slugs ---
    def _replace_slug(m: re.Match[str]) -> str:
        changes.append("account name → `<account-slug>`")
        return "<account-slug>"

    result = _KNOWN_ACCOUNT_SLUGS_RE.sub(_replace_slug, result)

    return result, changes


def redact_text(text: str) -> tuple[str, list[str]]:
    """Apply all PII redactions to a multi-line string.

    Returns:
        (redacted_text, changes) where changes is a list of human-readable
        descriptions of each substitution made (empty if nothing changed).
    """
    lines = text.splitlines(keepends=True)
    out_lines: list[str] = []
    all_changes: list[str] = []

    for line in lines:
        redacted, changes = _redact_line(line)
        out_lines.append(redacted)
        all_changes.extend(changes)

    # Preserve trailing newline presence/absence from the original
    redacted_text = "".join(out_lines)
    return redacted_text, all_changes


def has_pii(text: str) -> bool:
    """Return True if text contains any PII that would be redacted."""
    _, changes = redact_text(text)
    return len(changes) > 0


def summarize_changes(changes: list[str]) -> list[str]:
    """Collapse per-occurrence change descriptions into unique, counted lines.

    ``changes`` carries one entry per redaction (so ``len`` is the redaction
    count), but many share the same category string. For a human-facing notice
    we want each category once, with an ``(xN)`` suffix when it repeated —
    ``["account name -> `<account-slug>`", ...x3]`` becomes
    ``["account name -> `<account-slug>` (x3)"]``. Order of first appearance is
    preserved. Descriptions never contain the original PII value, so this stays
    safe to post publicly.
    """
    counts: dict[str, int] = {}
    for desc in changes:
        counts[desc] = counts.get(desc, 0) + 1
    return [d if n == 1 else f"{d} (x{n})" for d, n in counts.items()]
