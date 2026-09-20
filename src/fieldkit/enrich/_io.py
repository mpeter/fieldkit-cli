"""Shared I/O helpers for the enrich package.

Provides a single canonical location for loading/saving contact data files.

All enrichment state (contacts-raw.json, contacts-enriched.json, etc.) is
stored under ``<fieldkit_home>/contact-enrich/`` so it persists across tool
reinstalls and is accessible to the user.

Contact memory files are written to ``<fieldkit_home>/memory/personal/contacts/``
(the canonical personal-memory location) rather than the legacy
``<fieldkit_home>/contact-enrich/memory/`` directory.

Filename constants (kebab-case):

- ``CONTACTS_RAW`` — ``contacts-raw.json``
- ``CONTACTS_ENRICHED`` — ``contacts-enriched.json``
- ``WEB_SEARCH_RESULTS`` — ``web-search-results.json``
- ``CONTACTS_RAW_PREV`` — ``.contacts-raw-prev.json``
"""

import json
import logging
import shutil
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_fieldkit_home

log = logging.getLogger(__name__)

# Kebab-case filenames for all enrichment runtime artifacts.
CONTACTS_RAW: str = "contacts-raw.json"
CONTACTS_ENRICHED: str = "contacts-enriched.json"
WEB_SEARCH_RESULTS: str = "web-search-results.json"
CONTACTS_RAW_PREV: str = ".contacts-raw-prev.json"


@cache
def _enrich_dir() -> Path:
    """Canonical directory for contact enrichment data under the configured fieldkit home.

    Uses ``<fieldkit_home>/contact-enrich/`` so state survives tool reinstalls.
    The directory is created on first access.
    """
    d = get_fieldkit_home() / "contact-enrich"
    d.mkdir(parents=True, exist_ok=True)
    return d


@cache
def contacts_memory_dir() -> Path:
    """Canonical directory for per-contact memory files.

    Uses ``<fieldkit_home>/memory/personal/contacts/`` — the shared personal-memory
    tree — so contact files are co-located with other personal memory and are
    accessible to tools that scan that tree.
    The directory is created on first access.
    """
    d = get_fieldkit_home() / "memory" / "personal" / "contacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_legacy_memory_files() -> int:
    """Move contact files from the legacy contact-enrich/memory/ dir to contacts_memory_dir().

    Files are moved rather than copied; on filename collision the newer file wins.
    Returns the number of files migrated.
    """
    legacy_dir = get_fieldkit_home() / "contact-enrich" / "memory"
    if not legacy_dir.exists():
        return 0

    dest_dir = contacts_memory_dir()
    migrated = 0

    for src in legacy_dir.glob("*.md"):
        dest = dest_dir / src.name
        if dest.exists() and src.stat().st_mtime <= dest.stat().st_mtime:
            # Dest is newer — drop the legacy copy
            log.debug("migrate_legacy_memory_files: skipping %s (dest is newer)", src.name)
            src.unlink()
            continue
        shutil.move(str(src), dest)
        log.debug("migrate_legacy_memory_files: moved %s → %s", src, dest)
        migrated += 1

    # Remove the legacy directory if now empty
    try:
        legacy_dir.rmdir()
        log.debug("migrate_legacy_memory_files: removed empty legacy dir %s", legacy_dir)
    except OSError:
        pass  # Not empty or already gone — fine

    return migrated


enrich_dir = _enrich_dir


def load_raw_contacts() -> list[dict[str, Any]]:
    """Load contacts from contacts-raw.json. Returns [] if file is missing."""
    raw_file = _enrich_dir() / CONTACTS_RAW
    if not raw_file.exists():
        return []
    result: list[dict[str, Any]] = json.loads(raw_file.read_text(encoding="utf-8"))
    return result


def load_enriched_contacts() -> list[dict[str, Any]]:
    """Load contacts from contacts-enriched.json. Returns [] if file is missing."""
    enriched_file = _enrich_dir() / CONTACTS_ENRICHED
    if not enriched_file.exists():
        return []
    result: list[dict[str, Any]] = json.loads(enriched_file.read_text(encoding="utf-8"))
    return result
