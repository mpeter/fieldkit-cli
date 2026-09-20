#!/usr/bin/env python3
"""Pre-commit hook: block personal/user-specific data in staged files.

Catches:
  - Absolute paths containing a username (/home/<user>/, /Users/<user>/)
  - Hardcoded personal email addresses (non-placeholder patterns)
  - Hardcoded Slack usernames via from:<username> patterns

Exceptions (not flagged):
  - Placeholder patterns: YOUR_EMAIL, your-email, <email>, example.com, etc.
  - Test fixture emails that use RFC-reserved example domains
  - Intentional config keys like user_google_email used as dict keys (not hardcoded values)
  - Comments or headings — checked by content heuristic

This hook is intentionally strict. If a genuine exception is needed, add an
inline `# pii-guard: ignore` comment on that line.

Exit 1 = block commit. Exit 0 = allow.
"""

import errno
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Approved synthetic fixture slugs — fake stand-in accounts, SAFE to use in
# tests, fixtures, docs, and commit messages. NEVER flagged, even if a
# same-named entry also appears in accounts.yaml. Their ``.com`` forms are
# likewise allowlisted in _SAFE_EMAIL_DOMAIN. (R23: these are documented fake
# fixtures, not real customers — the commit-msg help text below points authors
# at them precisely so real names stay out of history.)
# ---------------------------------------------------------------------------
_FIXTURE_ACCOUNT_SLUGS: frozenset[str] = frozenset(
    [
        "acme-corp",  # pii-guard: ignore — approved synthetic fixture slug
        "acme-bank",  # pii-guard: ignore — approved synthetic fixture slug
        "midwest-ins",  # pii-guard: ignore — approved synthetic fixture slug
        "globalpay",  # pii-guard: ignore — approved synthetic fixture slug
    ]
)

_SEED_FILENAME = "pii-guard-seeds.json"
_MAX_SEED_BYTES = 64 * 1024
_ACCOUNT_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class _SeedFileError(RuntimeError):
    """A private seed exists but cannot be trusted."""


@dataclass(frozen=True)
class _AccountSlugState:
    pattern: re.Pattern[str]
    warnings: tuple[str, ...]


def _load_seed_slugs() -> tuple[frozenset[str], bool]:
    """Load private account slugs, returning ``(slugs, seed_missing)``.

    The fixed diagnostics raised here never contain a path, file content, or
    configured slug. Existing files fail closed when they cannot be securely
    inspected; an absent optional file is handled as degraded mode by the
    caller.
    """
    try:
        from fieldkit.config import get_fieldkit_data  # pii-guard: ignore

        seed_path = get_fieldkit_data() / _SEED_FILENAME
    except Exception as exc:
        raise _SeedFileError("runtime seed location is unavailable") from exc

    # O_NONBLOCK prevents a hostile FIFO at the fixed path from hanging every
    # commit hook before fstat() can reject the non-regular descriptor.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(seed_path, flags)
    except FileNotFoundError:
        return frozenset(), True
    except OSError as exc:
        category = "file type is unsafe" if exc.errno == errno.ELOOP else "file is unreadable"
        raise _SeedFileError(f"runtime seed {category}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _SeedFileError("runtime seed file type is unsafe")
        if metadata.st_uid != os.geteuid():
            raise _SeedFileError("runtime seed owner is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _SeedFileError("runtime seed permissions must be 0600")
        with os.fdopen(descriptor, encoding="utf-8") as seed_file:
            descriptor = -1
            text = seed_file.read(_MAX_SEED_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise _SeedFileError("runtime seed is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(text.encode("utf-8")) > _MAX_SEED_BYTES:
        raise _SeedFileError("runtime seed exceeds the size limit")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _SeedFileError("runtime seed schema is invalid") from exc
    if not isinstance(raw, list) or not all(
        isinstance(value, str) and _ACCOUNT_SLUG_RE.fullmatch(value) for value in raw
    ):
        raise _SeedFileError("runtime seed schema is invalid")
    return frozenset(value.lower() for value in raw), False


# ---------------------------------------------------------------------------
# Patterns that indicate personal data
# ---------------------------------------------------------------------------

# Absolute home-directory paths with a specific username
# Matches <user-home-path>/ or <user-home-path>/ but NOT /home/ alone or generic paths
_ABS_HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z][A-Za-z0-9_.-]{1,30}/")  # pii-guard: ignore

# from:<username> Slack search pattern with a real-looking username
_SLACK_FROM_RE = re.compile(r'from:[a-z][a-z0-9_.-]{2,30}(?:"|\s|$)', re.IGNORECASE)

# Hardcoded personal email: user@company.example.com where it looks like a real address
# but is NOT a placeholder
_PERSONAL_EMAIL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9._%+-]{2,30})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def _build_account_slug_pattern() -> _AccountSlugState:
    """Build a regex that matches any known private account slug.

    Loads account slugs from accounts.yaml at hook startup and unions them with
    the private runtime seed. Degrades to either source when the other is absent.

    Word-boundary match so "globalpay" in "provision" or "advisory" does not trigger.
    """
    seed_slugs, seed_missing = _load_seed_slugs()
    slugs: set[str] = set(seed_slugs)
    try:
        # Attempt to load live account slugs from the fieldkit config.
        # pii-guard: ignore — importing fieldkit config, not using account names as values
        import sys as _sys

        _repo_root = Path(__file__).resolve().parents[1]
        if str(_repo_root) not in _sys.path:
            _sys.path.insert(0, str(_repo_root))
        from fieldkit.config import get_account_names  # pii-guard: ignore

        for name in get_account_names():
            if name and isinstance(name, str):
                slugs.add(name.lower())
    except Exception:  # noqa: BLE001
        # Config unavailable (no fieldkit installed, no accounts.yaml, etc.)
        # Fall back to the hardcoded seed set — detection is still active.
        pass

    # Approved synthetic fixtures are never flagged, even if accounts.yaml lists
    # one (e.g. a demo account). Subtract them after the union so no source can
    # re-block a fixture slug.
    slugs -= _FIXTURE_ACCOUNT_SLUGS

    if not slugs:
        # No real slugs to detect. An empty alternation (r"\b()\b") would match
        # at every word boundary; return a pattern that never matches instead.
        return _AccountSlugState(
            re.compile(r"(?!)"),
            ("pii-guard: no runtime account slugs are active; other PII checks remain enabled.",),
        )

    # Build alternation pattern; escape each slug for regex safety.
    # Use a plain (non-verbose) pattern so the alternation is unambiguous.
    alternation = "|".join(re.escape(s) for s in sorted(slugs))
    warnings: tuple[str, ...] = (
        ("pii-guard: optional runtime account-slug seed is absent; using live account configuration.",)
        if seed_missing
        else ()
    )
    return _AccountSlugState(re.compile(rf"\b({alternation})\b", re.IGNORECASE), warnings)


# Populated once by main() before scanning. The never-match default keeps direct
# unit calls deterministic without touching operator configuration at import.
_KNOWN_ACCOUNT_SLUGS_RE: re.Pattern[str] = re.compile(r"(?!)")

# ---------------------------------------------------------------------------
# Allowlists — patterns that are NOT violations
# ---------------------------------------------------------------------------

# Email local-parts that are obviously placeholders. A human-like local part is
# not safe on its own: it still needs an RFC-reserved example domain below.
_SAFE_EMAIL_LOCAL = re.compile(
    r"""(?xi)
    ^(
        YOUR[_-]?EMAIL | your[_-]?email | your[_-]?name |
        user | name | example | test | placeholder | email |
        someone | anyone
    )$
    """,
    re.IGNORECASE,
)

# Email domains that are safe because RFC 2606 reserves them for examples.
_SAFE_EMAIL_DOMAIN = re.compile(
    r"""(?xi)
    ^(?:[a-z0-9-]+\.)*example\.(?:com|org|net)$
    """,
    re.IGNORECASE,
)

# Lines containing these strings are assumed to be safe (dict keys, type annotations,
# schema definitions, comments that use the keyword generically)
_SAFE_LINE_MARKERS = [
    "user_google_email",  # MCP tool parameter name, not a value
    "# pii-guard: ignore",  # explicit opt-out — Python/shell comment form
    "pii-guard: ignore",  # explicit opt-out — also matches HTML comment form <!-- pii-guard: ignore -->
    "<account-slug>",  # already-replaced placeholder — not a violation
    "<Account Name>",  # display name placeholder
    "YOUR_EMAIL",
    "your-email",
    "<email>",
    "example.com",
    "placeholder",
    "mailto:email@domain",  # template table cells
    "gemini-notes@google.com",  # known system sender
    "notifications@github.com",  # known system sender
    "bounce+",  # email bounce headers
    "RFC Message-ID",
    "{{",  # template variable pattern — e.g. {{primary_account}} in evals.json
]

# File extensions to skip entirely
_SKIP_EXTENSIONS = {".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}

# Directories to skip
_SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "build", "dist"}

# Path prefixes where the account-slug check is suppressed — planning/spec artifacts
# necessarily reference real infrastructure hostnames (e.g., auth.redhat.com) that
# match account slugs in the known-slugs list. Only the slug check is suppressed;
# home-path, personal-email, and Slack-pattern checks remain active for these files.
# NOTE: `filepath` from git diff is always repo-root-relative, so startswith() is reliable.
# Scope: openspec/changes/ only captures the active change artifacts. Future openspec/specs/
# files (if they reference infra hostnames) would also benefit from this suppression.
_SLUG_SKIP_PATH_PREFIXES = {
    "openspec/",
    # specs/ are planning/speckit artifacts — same rationale as openspec/. They
    # necessarily reference real account slugs as the subject of the work being
    # planned. Only the slug check is suppressed; home-path and personal-email
    # checks remain active.
    "specs/",
    # shadowbot module necessarily references Red Hat infrastructure URLs
    # (auth.redhat.com, shadowbot.io.redhat.com, api.enterprise.redhat.com).
    # These are company infrastructure hostnames, not customer account slugs.
    "src/fieldkit/commands/shadowbot/",
    "tests/test_shadowbot_",
    # docs/, scripts/, and CHANGELOG.md may reference Red Hat infrastructure
    # hostnames in auto-generated CLI reference, runbook content, and changelog
    # entries. Only the slug check is suppressed; home-path and personal-email
    # checks remain active.
    "docs/",
    "scripts/generate_cli_docs.py",
    "CHANGELOG.md",
}

# Specific files to skip — the pii_guard test file necessarily exercises the
# detection patterns and must be exempt from its own checks.
_SKIP_FILES = {"tests/test_pii_guard.py", "tests/test_pii_patterns.py", "hooks/pii_guard.py"}


def _is_safe_email(local: str, domain: str) -> bool:
    """Return True if this email looks like a placeholder or test address."""
    if _SAFE_EMAIL_LOCAL.match(local):
        return True
    return bool(_SAFE_EMAIL_DOMAIN.match(domain))


def _check_line(
    line: str,
    lineno: int,
    filepath: str,
    account_slug_pattern: re.Pattern[str] | None = None,
) -> list[str]:
    """Return a list of violation messages for this line (empty = clean)."""
    violations: list[str] = []

    # Skip lines with safe markers
    for marker in _SAFE_LINE_MARKERS:
        if marker.lower() in line.lower():
            return []

    # Check absolute home-directory paths
    if _ABS_HOME_RE.search(line):
        violations.append(
            f"{filepath}:{lineno}: absolute home-dir path — use a placeholder like <user-home-path> or Path(__file__)"
        )

    # Check Slack from: patterns
    m = _SLACK_FROM_RE.search(line)
    if m:
        # Allow 'from:your-username' or 'from:user'
        token = m.group(0).split(":")[1].rstrip("\"' ").lower()
        if token not in ("your-username", "user", "username", "you"):
            violations.append(f"{filepath}:{lineno}: Slack 'from:{token}' looks personal — use 'from:your-username'")

    # Check personal email addresses
    for match in _PERSONAL_EMAIL_RE.finditer(line):
        local, domain = match.group(1), match.group(2)
        if not _is_safe_email(local, domain):
            violations.append(f"{filepath}:{lineno}: personal email '{local}@{domain}' — use a placeholder")

    # Check for hardcoded real account slugs (R23)
    # Suppressed for openspec/ files — spec artifacts reference real infrastructure
    # hostnames (e.g., auth.redhat.com) that match account slugs; this is intentional.
    slug_suppressed = any(filepath.startswith(p) for p in _SLUG_SKIP_PATH_PREFIXES)
    if not slug_suppressed:
        pattern = _KNOWN_ACCOUNT_SLUGS_RE if account_slug_pattern is None else account_slug_pattern
        if pattern.search(line):
            violations.append(f"{filepath}:{lineno}: real account slug detected — use <account-slug> placeholder (R23)")

    return violations


def _get_staged_diff() -> list[tuple[str, int, str]]:
    """Return list of (filepath, lineno, added_line) for all staged additions."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return []

    lines: list[tuple[str, int, str]] = []
    current_file = ""
    current_lineno = 0

    for raw_line in result.stdout.splitlines():
        if raw_line.startswith("diff --git"):
            # Extract filename
            parts = raw_line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else ""
        elif raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
        elif raw_line.startswith("@@ "):
            # @@ -a,b +c,d @@
            m = re.search(r"\+(\d+)", raw_line)
            current_lineno = int(m.group(1)) if m else 0
        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            # Added line
            ext = Path(current_file).suffix.lower()
            parent = Path(current_file).parts[0] if Path(current_file).parts else ""
            if ext not in _SKIP_EXTENSIONS and parent not in _SKIP_DIRS and current_file not in _SKIP_FILES:
                lines.append((current_file, current_lineno, raw_line[1:]))
            current_lineno += 1
        elif not raw_line.startswith("-"):
            current_lineno += 1

    return lines


def _check_commit_msg_files(paths: list[str], account_slug_pattern: re.Pattern[str] | None = None) -> int:
    """Scan commit-message file(s) for PII.

    Commit messages are published, shared history — a real account slug, personal
    email, home path, or Slack handle written into one lands on ``main`` verbatim
    under rebase merges and cannot be scrubbed without a destructive history
    rewrite. This closes the gap that let a representative example slug reach a commit
    message even though the file-content guard was clean.

    Git comment lines (leading ``#``) and the diff/verbose section that
    ``commit --verbose`` appends below the scissors line are ignored, so only the
    author's own message text is checked.
    """
    all_violations: list[str] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Everything below the commit --verbose scissors line is the diff, not
            # the author's message — stop scanning there. Checked before the
            # generic '#' skip since the scissors line itself starts with '#'.
            if line.startswith("# ------------------------ >8"):
                break
            if line.startswith("#"):
                continue
            all_violations.extend(_check_line(line, lineno, "<commit-msg>", account_slug_pattern))

    if all_violations:
        print(
            "pii-guard: Personal/user-specific data detected in the commit message.",
            file=sys.stderr,
        )
        print(
            "  Commit messages are permanent, shared history — keep real account",
            file=sys.stderr,
        )
        print(
            "  slugs, personal emails, and home paths out. Use a placeholder or a",
            file=sys.stderr,
        )
        print(
            "  fictional stand-in (e.g. acme-corp, <account-slug>).\n",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    try:
        account_slugs = _build_account_slug_pattern()
    except _SeedFileError as exc:
        print(f"pii-guard: blocked because {exc}.", file=sys.stderr)
        return 1
    for warning in account_slugs.warnings:
        print(warning, file=sys.stderr)

    # Commit-msg mode: pre-commit's commit-msg stage passes the message file path.
    if args and args[0] == "--commit-msg":
        return _check_commit_msg_files(args[1:], account_slugs.pattern)

    added_lines = _get_staged_diff()
    if not added_lines:
        return 0

    all_violations: list[str] = []
    for filepath, lineno, line in added_lines:
        violations = _check_line(line, lineno, filepath, account_slugs.pattern)
        all_violations.extend(violations)

    if all_violations:
        print(
            "pii-guard: Personal/user-specific data detected in staged changes.",
            file=sys.stderr,
        )
        print(
            "  This repo must remain agnostic to any specific user's identity.",
            file=sys.stderr,
        )
        print(
            "  Use placeholders like YOUR_EMAIL, <user-home-path>, your-username.",
            file=sys.stderr,
        )
        print(
            "  Add '# pii-guard: ignore' to a line to explicitly exempt it.\n",
            file=sys.stderr,
        )
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
