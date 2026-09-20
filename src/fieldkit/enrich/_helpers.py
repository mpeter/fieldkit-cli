"""Shared helper functions for the enrich package.

Provides pure filtering functions (transactional domain detection, contact
exclusion), atomic JSON I/O, checkpoint persistence, and filename normalisation.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fieldkit.enrich._io import enrich_dir
from fieldkit.enrich.schema import EnrichmentCheckpoint

# ---------------------------------------------------------------------------
# Identity / transactional exclusion
# ---------------------------------------------------------------------------

#: Domain patterns that indicate automated/transactional senders, not real contacts.
_TRANSACTIONAL_DOMAIN_PATTERNS: frozenset[str] = frozenset(
    {
        "docusign.com",
        "docusign.net",
        "echosign.com",
        "noreply.github.com",
        "notifications.google.com",
        "mail.workday.com",
        "workdaymail.com",
        "salesforce.com",
        "exacttarget.com",
        "marketo.net",
        "mailchimp.com",
        "sendgrid.net",
        "amazonses.com",
        "bounce.com",
    }
)

#: Local-part prefixes that indicate automated senders.
_TRANSACTIONAL_LOCAL_PREFIXES: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "notifications",
    "mailer-daemon",
    "postmaster",
    "bounces",
    "bounce+",
    "automated",
    "system",
    "alert",
    "alerts",
    "support",
)


def _is_transactional_domain(email: str) -> bool:
    """Return True if *email* comes from a known transactional/automated domain.

    Checks the full domain (and any subdomain of it) and the local-part prefix.

    Args:
        email: The email address to test.

    Returns:
        True if the email is from a transactional/automated source, False otherwise.
    """
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    domain = domain.lower().strip()
    local = local.lower().strip()

    if any(domain == pattern or domain.endswith(f".{pattern}") for pattern in _TRANSACTIONAL_DOMAIN_PATTERNS):
        return True

    # Prefix-based check (covers noreply@any-domain.example.com patterns)
    for prefix in _TRANSACTIONAL_LOCAL_PREFIXES:
        if local == prefix or local.startswith(f"{prefix}+") or local.startswith(f"{prefix}@"):
            return True

    return False


def _should_skip_contact(email: str | None, user_email: str | None) -> bool:
    """Return True if this contact should be excluded from enrichment.

    Skips:
    - Contacts with no email (cannot apply exclusion rules — not skipped here).
    - The user's own email address (case-insensitive).
    - Any email from a known transactional domain or with an automated local-part.

    Args:
        email: The contact's email address, or None if unknown.
        user_email: The authenticated user's own email address, or None to skip
            the identity check.

    Returns:
        True if the contact should be excluded, False otherwise.
    """
    if not email:
        return False
    email_lower = email.lower().strip()
    if user_email and email_lower == user_email.lower().strip():
        return True
    return _is_transactional_domain(email)


# ---------------------------------------------------------------------------
# Atomic JSON I/O
# ---------------------------------------------------------------------------


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to *path* atomically via temp file + rename.

    Prevents partial writes: the destination file is only replaced once the
    full content has been flushed to a sibling temp file.  On any exception
    the temp file is cleaned up before re-raising.

    Args:
        path: Destination file path.
        data: JSON-serialisable value to write.

    Raises:
        OSError: If the temp file cannot be written or renamed.
        TypeError: If *data* is not JSON-serialisable.
    """
    content = json.dumps(data, indent=2)
    # Write to a sibling temp file so the rename is atomic on POSIX filesystems.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        Path(tmp).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------


def load_checkpoint() -> EnrichmentCheckpoint | None:
    """Load the enrichment checkpoint from disk if it exists.

    Returns:
        An :class:`EnrichmentCheckpoint` instance if a checkpoint file is
        present, or ``None`` if no checkpoint has been saved yet.
    """
    checkpoint_file = enrich_dir() / "checkpoint.json"
    if not checkpoint_file.exists():
        return None

    try:
        data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        return EnrichmentCheckpoint(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        import warnings

        warnings.warn(
            f"Corrupt checkpoint at {checkpoint_file}: {exc}. Starting fresh.",
            stacklevel=2,
        )
        return None


def save_checkpoint(checkpoint: EnrichmentCheckpoint) -> None:
    """Persist *checkpoint* to disk atomically.

    Args:
        checkpoint: The checkpoint to save.
    """
    checkpoint_file = enrich_dir() / "checkpoint.json"
    _write_json_atomic(checkpoint_file, checkpoint.model_dump())


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def normalize_name_for_filename(name: str) -> str:
    """Convert *name* to a safe, lowercase filename fragment.

    Replaces spaces with underscores and strips dots and commas.

    Args:
        name: The display name to normalise.

    Returns:
        A lowercase string suitable for use in a filename.

    Examples:
        >>> normalize_name_for_filename("John Smith")
        'john_smith'
        >>> normalize_name_for_filename("Dr. Jane, PhD")
        'dr_jane_phd'
    """
    return name.lower().replace(" ", "_").replace(".", "").replace(",", "")
