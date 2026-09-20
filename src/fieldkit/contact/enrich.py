"""fieldkit.contact.enrich — contact discovery, web-result merge, and record enrichment.

Merges the previous ``commands/enrich/{contacts,discover_contacts,apply_web_results,
enrich_contacts}.py`` Click adapters into one Click-free domain module backing
``fieldkit contact enrich``. Parsing/pipeline internals live in the private
``_enrich_helpers`` module; this file exposes the three write operations:

- :func:`discover` — scan vault files + Gmail cache for contacts, write
  ``contacts-raw.json`` (was ``enrich contacts`` / ``enrich discover``).
- :func:`apply_web` — merge ``web-search-results.json`` into ``contacts-raw.json``
  (was ``enrich apply-web``).
- :func:`enrich_records` — run the full batch enrichment pipeline, writing
  ``contacts-enriched.json`` and per-contact memory files (was ``enrich enrich``).

CLI presentation (human-readable vs ``--json``) lives in
``fieldkit.commands.contact.enrich_cmd`` — these functions return data and raise;
they do not print.
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fieldkit.contact._enrich_helpers import discover_all_contacts, merge_web_results, run_enrichment_pipeline
from fieldkit.enrich._io import (
    CONTACTS_RAW,
    CONTACTS_RAW_PREV,
    enrich_dir,
    load_raw_contacts,
    migrate_legacy_memory_files,
)


def _last_run_file() -> Path:
    return enrich_dir() / ".last-run"


def _count_contacts(path: Path) -> int:
    """Return number of contacts in a JSON array file, or 0 on error."""
    try:
        result: list[Any] = json.loads(path.read_text(encoding="utf-8"))
        return len(result)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


@dataclass(frozen=True)
class DiscoverResult:
    """Summary of a :func:`discover` run."""

    total: int
    with_email: int
    with_linkedin: int
    new_since_last_run: int | None
    by_source: dict[str, int] = field(default_factory=dict)
    by_account: dict[str, int] = field(default_factory=dict)


def discover(*, account: str | None = None) -> DiscoverResult:
    """Discover contacts from all sources and write ``contacts-raw.json``.

    Args:
        account: Restrict discovery to a single account slug. ``None`` scans
            every account.

    Returns:
        A :class:`DiscoverResult` summary of the run.
    """
    contacts_raw_path = enrich_dir() / CONTACTS_RAW
    contacts_raw_prev_path = enrich_dir() / CONTACTS_RAW_PREV
    first_run = not _last_run_file().exists()
    prev_count = _count_contacts(contacts_raw_prev_path) if not first_run else None

    contacts = discover_all_contacts(account=account)
    contacts_raw_path.write_text(json.dumps(contacts, indent=2), encoding="utf-8")

    # Backup for next run's comparison.
    if contacts_raw_path.exists():
        shutil.copy2(contacts_raw_path, contacts_raw_prev_path)

    _last_run_file().write_text(datetime.now().astimezone().isoformat(), encoding="utf-8")

    new_since_last_run = None if prev_count is None else max(len(contacts) - prev_count, 0)

    by_source: dict[str, int] = {}
    by_account: dict[str, int] = {}
    with_email = 0
    with_linkedin = 0
    for contact in contacts:
        source = contact.get("source", "unknown")
        acct = contact.get("account", "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        by_account[acct] = by_account.get(acct, 0) + 1
        if contact.get("email"):
            with_email += 1
        if contact.get("linkedin_url"):
            with_linkedin += 1

    return DiscoverResult(
        total=len(contacts),
        with_email=with_email,
        with_linkedin=with_linkedin,
        new_since_last_run=new_since_last_run,
        by_source=by_source,
        by_account=by_account,
    )


@dataclass(frozen=True)
class ApplyWebResult:
    """Summary of an :func:`apply_web` run."""

    updated_fields: int
    web_results_applied: int
    total_raw_contacts: int


def apply_web(*, account: str | None = None) -> ApplyWebResult:
    """Merge ``web-search-results.json`` into ``contacts-raw.json``.

    Args:
        account: Restrict the merge to web results for a single account slug.
            ``None`` applies every result in the file.

    Returns:
        An :class:`ApplyWebResult` summary of the merge.
    """
    from fieldkit.enrich._io import WEB_SEARCH_RESULTS

    raw_contacts = load_raw_contacts()
    results_file = enrich_dir() / WEB_SEARCH_RESULTS
    web_results: list[dict[str, Any]] = (
        json.loads(results_file.read_text(encoding="utf-8")) if results_file.exists() else []
    )

    if account is not None:
        web_results = [r for r in web_results if r.get("account") == account]

    if not web_results:
        return ApplyWebResult(updated_fields=0, web_results_applied=0, total_raw_contacts=len(raw_contacts))

    updated_contacts, updated_fields = merge_web_results(raw_contacts, web_results)

    raw_file = enrich_dir() / CONTACTS_RAW
    raw_file.write_text(json.dumps(updated_contacts, indent=2), encoding="utf-8")

    return ApplyWebResult(
        updated_fields=updated_fields,
        web_results_applied=len(web_results),
        total_raw_contacts=len(updated_contacts),
    )


@dataclass(frozen=True)
class EnrichRecordsResult:
    """Summary of an :func:`enrich_records` run."""

    total_enriched: int
    total_failed: int
    migrated_legacy_files: int
    total_raw_contacts: int


def enrich_records(*, account: str | None = None) -> EnrichRecordsResult:
    """Run the full enrichment pipeline over ``contacts-raw.json``.

    Requires web search results to already have been applied via
    :func:`apply_web` for contacts that need one. Writes
    ``contacts-enriched.json`` and per-contact memory files.

    Args:
        account: Restrict the run to raw contacts belonging to this account
            slug. ``None`` processes every raw contact.

    Note:
        Checkpoint/resume (``checkpoint.json``) tracks progress against the
        full raw-contacts list ordering; scoping to a single ``account`` runs
        an independent pass rather than resuming a prior filtered run.

    Returns:
        An :class:`EnrichRecordsResult` summary of the run.
    """
    migrated = migrate_legacy_memory_files()

    raw_contacts = load_raw_contacts()
    if account is not None:
        raw_contacts = [c for c in raw_contacts if c.get("account") == account]

    if not raw_contacts:
        return EnrichRecordsResult(
            total_enriched=0, total_failed=0, migrated_legacy_files=migrated, total_raw_contacts=0
        )

    total_enriched, total_failed = run_enrichment_pipeline(raw_contacts)

    return EnrichRecordsResult(
        total_enriched=total_enriched,
        total_failed=total_failed,
        migrated_legacy_files=migrated,
        total_raw_contacts=len(raw_contacts),
    )
