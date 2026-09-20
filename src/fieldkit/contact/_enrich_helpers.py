"""Private implementation details for :mod:`fieldkit.contact.enrich`.

Split out of ``enrich.py`` purely to keep that module under the repo's
~500-line guidance after merging the four legacy ``commands/enrich/*.py``
leaves. Not part of the public API — import from ``fieldkit.contact.enrich``.
"""

import json
import logging
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fieldkit.config import build_domain_account_map, get_accounts_root, get_internal_domains, get_user_email
from fieldkit.enrich._helpers import _should_skip_contact, _write_json_atomic, normalize_name_for_filename
from fieldkit.enrich._io import contacts_memory_dir, enrich_dir
from fieldkit.enrich.constants import GARBAGE_NAMES as _GARBAGE_NAMES
from fieldkit.enrich.schema import ContactRecord
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.query_domain import connect_read_only, prepare_database, query_by_email
from fieldkit.pursuit.io import extract_frontmatter_text

log = logging.getLogger(__name__)

BATCH_SIZE = 5
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Discovery: account.md / SF frontmatter / Gmail cache extraction
# ---------------------------------------------------------------------------

_RE_BOLD_NAME = re.compile(r"\*\*([^*]+)\*\*")
_RE_PAREN_TITLE = re.compile(r"\(([^)]+)\)")
_RE_EMAIL = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")
_RE_LINKEDIN = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[\w-]+")
_RE_STRIP_MARKUP = re.compile(r"\*\*|\[|\]")
_RE_STRIP_MARKUP_PARENS = re.compile(r"\*\*|\[|\]|\(.*?\)")
_RE_CONTACT_ROLES = re.compile(r"sf_contact_roles:\s*\n((?:  -.*\n)*)")
_RE_PAREN_TITLE_END = re.compile(r"\(([^)]+)\)$")

_COL_EXACT: dict[str, str] = {
    "name": "name",
    "title": "title",
    "supplemental": "supplemental",
    "sf contact role": "sf_role",
}


def _classify_col(col_name: str) -> str | None:
    """Return the canonical key for a header cell name, or None if not recognized."""
    exact = _COL_EXACT.get(col_name)
    if exact:
        return exact
    if "contact role" in col_name:
        return "sf_role"
    return None


def _parse_table_header(cells: list[str]) -> dict[str, int]:
    """Map column names to cell indices from a pipe-table header row."""
    indices: dict[str, int] = {}
    for i, cell in enumerate(cells):
        key = _classify_col(cell.strip().lower())
        if key:
            indices[key] = i
    return indices


def _cell_value(cells: list[str], col_indices: dict[str, int], key: str, default_idx: int) -> str:
    """Extract a cell value by column key or fallback index."""
    idx = col_indices.get(key, default_idx)
    return cells[idx] if idx < len(cells) else ""


def _clean_sf_role(raw: str) -> str | None:
    """Normalize an SF role cell value, returning None for placeholders."""
    if not raw or raw in ("—", "-", "[IDENTIFY]"):
        return None
    return _RE_STRIP_MARKUP_PARENS.sub("", raw).strip() or None


def _table_row_to_contact(
    cells: list[str],
    col_indices: dict[str, int],
    account_name: str,
) -> dict[str, Any] | None:
    """Convert a parsed table row (list of cells) to a contact dict, or None to skip."""
    name = _RE_STRIP_MARKUP.sub("", _cell_value(cells, col_indices, "name", 1)).strip()
    if not name or name in ("—", "-") or name in _GARBAGE_NAMES:
        return None

    title_raw = _RE_STRIP_MARKUP.sub("", _cell_value(cells, col_indices, "title", 2)).strip()
    sf_role_raw = _RE_STRIP_MARKUP.sub("", _cell_value(cells, col_indices, "sf_role", 3)).strip()
    supplemental = _cell_value(cells, col_indices, "supplemental", 4)

    email_match = _RE_EMAIL.search(supplemental + " " + name)
    linkedin_match = _RE_LINKEDIN.search(supplemental)

    return {
        "full_name": name,
        "title": title_raw if title_raw and title_raw not in ("—", "-") else None,
        "email": email_match.group(0) if email_match else None,
        "linkedin_url": linkedin_match.group(0) if linkedin_match else None,
        "sf_role": _clean_sf_role(sf_role_raw),
        "company": account_name.replace("-", " ").title(),
        "account": account_name,
        "source": "account-file",
    }


def _parse_table_contacts(lines: list[str], account_name: str) -> list[dict[str, Any]]:
    """Parse table-format stakeholder rows into contact dicts."""
    contacts: list[dict[str, Any]] = []
    in_table = False
    col_indices: dict[str, int] = {}

    for raw_line in lines:
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        if line.startswith("| Name"):
            col_indices = _parse_table_header([cell.strip() for cell in line.split("|")])
            in_table = True
            continue
        if line.startswith("|---") or line.startswith("| ---"):
            continue
        if not in_table:
            continue

        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4:
            continue

        contact = _table_row_to_contact(cells, col_indices, account_name)
        if contact is not None:
            contacts.append(contact)

    return contacts


def _parse_list_contacts(lines: list[str], account_name: str) -> list[dict[str, Any]]:
    """Parse list-format stakeholder entries into contact dicts."""
    contacts: list[dict[str, Any]] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or not line.startswith("-"):
            continue

        # Pattern: - **Name** (Title) — email or LinkedIn
        list_name_match = _RE_BOLD_NAME.search(line)
        if not list_name_match:
            continue

        name = list_name_match.group(1).strip()

        title_match = _RE_PAREN_TITLE.search(line)
        title = title_match.group(1).strip() if title_match else None

        email_match = _RE_EMAIL.search(line)
        email = email_match.group(0) if email_match else None

        linkedin_match = _RE_LINKEDIN.search(line)
        linkedin = linkedin_match.group(0) if linkedin_match else None

        contacts.append(
            {
                "full_name": name,
                "title": title,
                "email": email,
                "linkedin_url": linkedin,
                "company": account_name.replace("-", " ").title(),
                "account": account_name,
                "source": "account-file",
            }
        )

    return contacts


def extract_from_account_md(account_path: Path) -> list[dict[str, Any]]:
    """Extract contacts from account.md stakeholder map.

    Handles two formats:
    1. Table format with columns: Name | Title | SF Contact Role | Supplemental | ...
    2. List format: - **Jane Smith** (CFO) — jane.smith@example.com
    """
    account_file = account_path / "account.md"
    if not account_file.exists():
        return []

    account_name = account_path.name
    content = account_file.read_text(encoding="utf-8")

    stakeholder_section = re.search(
        r"##\s+Stakeholder(?:\s+Map)?.*?\n+(.*?)(?=\n## [^#]|\Z)",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if not stakeholder_section:
        return []

    section_text = stakeholder_section.group(1)

    # Remove coverage gaps / taxonomy subsections (not actual stakeholders)
    section_text = re.sub(
        r"###\s+Coverage Gaps.*?(?=\n###|\n##|\Z)",
        "",
        section_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    lines = section_text.split("\n")

    is_table = any("|" in line for line in lines[:5])
    if is_table:
        return _parse_table_contacts(lines, account_name)
    return _parse_list_contacts(lines, account_name)


def extract_from_sf_frontmatter(account_path: Path) -> list[dict[str, Any]]:
    """Extract contacts from SF contact roles in pursuit frontmatter."""
    contacts: list[dict[str, Any]] = []
    pursuits_dir = account_path / "pursuits"

    if not pursuits_dir.exists():
        return contacts

    account_name = account_path.name

    for pursuit_file in pursuits_dir.glob("*.md"):
        content = pursuit_file.read_text(encoding="utf-8")

        frontmatter = extract_frontmatter_text(content)
        if not frontmatter:
            continue

        roles_match = _RE_CONTACT_ROLES.search(frontmatter)
        if not roles_match:
            continue

        roles_text = roles_match.group(1)

        for raw_line in roles_text.split("\n"):
            line = raw_line.strip()
            if not line.startswith("-"):
                continue

            line = line[1:].strip()  # Remove leading dash

            parts = line.rsplit(" - ", 1)
            if len(parts) != 2:
                continue

            name_title = parts[0].strip()
            sf_role = parts[1].strip()

            title_match = _RE_PAREN_TITLE_END.search(name_title)
            if title_match:
                title = title_match.group(1).strip()
                name = name_title[: title_match.start()].strip()
            else:
                name = name_title
                title = None

            contacts.append(
                {
                    "full_name": name,
                    "title": title,
                    "company": account_name.replace("-", " ").title(),
                    "account": account_name,
                    "sf_role": sf_role,
                    "source": "sf",
                }
            )

    return contacts


def load_gmail_cache_contacts(account_name: str) -> list[dict[str, Any]]:
    """Load contact data by querying the gmail.db people table directly.

    Expects gmail cache to have been run already (people table populated).
    """
    db_path = get_gmail_db_path()
    if not db_path.exists():
        log.warning("Gmail cache DB not found at %s", db_path)
        return []

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT email, display_name, thread_count, last_seen
            FROM people
            WHERE account = ? AND (is_internal = 0 OR is_internal IS NULL)
            ORDER BY thread_count DESC
            """,
            (account_name,),
        ).fetchall()
        conn.close()

        contacts: list[dict[str, Any]] = []
        for row in rows:
            contacts.append(
                {
                    "full_name": row["display_name"] or "",
                    "email": row["email"] or "",
                    "company": account_name.replace("-", " ").title(),
                    "account": account_name,
                    "email_frequency": row["thread_count"] or 0,
                    "last_contact_date": row["last_seen"],
                    "source": "gmail",
                }
            )

        return contacts

    except sqlite3.Error as e:
        log.warning("Failed to query Gmail contacts for %s: %s", account_name, e)
        return []


def _merge_into_existing(existing: dict[str, Any], contact: dict[str, Any]) -> None:
    """Merge a contact into an existing entry in-place."""
    existing_sources: list[Any] = existing.get("_sources", [existing.get("source")])
    new_source = contact.get("source")
    if new_source and new_source not in existing_sources:
        existing_sources.append(new_source)
    existing["_sources"] = existing_sources

    for field, value in contact.items():
        if field == "source":
            continue
        if field == "email_frequency" and value:
            existing[field] = existing.get(field, 0) + value
        elif value and not existing.get(field):
            existing[field] = value


def _contact_key(contact: dict[str, Any]) -> str | None:
    """Return the deduplication key for a contact, or None if not identifiable."""
    email: Any = contact.get("email")
    if email:
        return str(email).lower()
    full_name: Any = contact.get("full_name")
    company: Any = contact.get("company")
    if full_name and company:
        return f"{str(full_name).lower()}|{str(company).lower()}"
    return None


def merge_contacts(all_contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate contacts from different sources.

    Matches on email (primary) or name+company. Combines data from all sources.
    """
    contact_map: dict[str, dict[str, Any]] = {}

    for contact in all_contacts:
        key = _contact_key(contact)
        if key is None:
            continue

        if key in contact_map:
            _merge_into_existing(contact_map[key], contact)
        else:
            contact["_sources"] = [contact.get("source")]
            contact_map[key] = contact

    # Priority: sf > backstory > gmail > account-file
    source_priority = {"sf": 4, "backstory": 3, "gmail": 2, "account-file": 1, "web": 0}

    merged = []
    for contact in contact_map.values():
        sources = contact.pop("_sources", [])
        contact["source"] = max(sources, key=lambda s: source_priority.get(s, 0))
        contact["_all_sources"] = sources
        merged.append(contact)

    return merged


def discover_all_contacts(*, account: str | None = None) -> list[dict[str, Any]]:
    """Scan all accounts (or a single one) and aggregate contacts from every source.

    Args:
        account: Restrict discovery to this account slug only. ``None`` scans
            every account directory under the accounts root.
    """
    all_contacts: list[dict[str, Any]] = []

    accounts_dir = get_accounts_root()
    if not accounts_dir.exists():
        log.warning("accounts/ directory not found: %s", accounts_dir)
        return []

    for account_path in sorted(accounts_dir.iterdir()):
        if not account_path.is_dir():
            continue
        # Dot-directories, including `.template`, are scaffolding rather than accounts.
        if account_path.name.startswith("."):
            continue
        if account is not None and account_path.name != account:
            continue

        account_name = account_path.name
        log.info("Discovering contacts in %s...", account_name)

        account_md_contacts = extract_from_account_md(account_path)
        log.info("  Found %d from account.md", len(account_md_contacts))
        all_contacts.extend(account_md_contacts)

        sf_contacts = extract_from_sf_frontmatter(account_path)
        log.info("  Found %d from SF frontmatter", len(sf_contacts))
        all_contacts.extend(sf_contacts)

        gmail_contacts = load_gmail_cache_contacts(account_name)
        log.info("  Found %d from Gmail cache", len(gmail_contacts))
        all_contacts.extend(gmail_contacts)

    log.info("Merging %d raw contacts...", len(all_contacts))
    merged_contacts = merge_contacts(all_contacts)
    log.info("Result: %d unique contacts", len(merged_contacts))

    return merged_contacts


# ---------------------------------------------------------------------------
# Web-results merge (contacts-raw.json <- web-search-results.json)
# ---------------------------------------------------------------------------


def merge_web_results(
    raw_contacts: list[dict[str, Any]], web_results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Merge web search results into raw contacts (mutates and returns raw_contacts)."""
    results_map = {}
    for result in web_results:
        key = (result.get("full_name", "").lower(), result.get("account", ""))
        results_map[key] = result

    updated_count = 0
    for contact in raw_contacts:
        key = (contact.get("full_name", "").lower(), contact.get("account", ""))

        if key in results_map:
            result = results_map[key]

            if result.get("linkedin_url") and not contact.get("linkedin_url"):
                contact["linkedin_url"] = result["linkedin_url"]
                updated_count += 1

            if result.get("email") and not contact.get("email"):
                contact["email"] = result["email"]
                updated_count += 1

            if result.get("phone") and not contact.get("phone"):
                contact["phone"] = result["phone"]
                updated_count += 1

    return raw_contacts, updated_count


# ---------------------------------------------------------------------------
# Full enrichment pipeline (contacts-raw.json -> contacts-enriched.json + memory/)
# ---------------------------------------------------------------------------


def search_web_for_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Mark contact as needing web enrichment.

    Actual web search should be done via MCP tools (tavily_search through
    mcpjungle) in a separate orchestration step, not via subprocess CLI calls.
    This function just marks the contact for later enrichment.
    """
    name = contact.get("full_name", "")
    company = contact.get("company", "")

    if not name or not company:
        return contact

    contact["_needs_web_enrichment"] = True
    contact["_search_query"] = f'"{name}" {company} LinkedIn email contact'
    log.info("Marked for web enrichment: %s", name)

    return contact


def query_gmail_cache(contact: dict[str, Any]) -> dict[str, Any]:
    """Query Gmail cache for engagement signals via direct function call."""
    email = contact.get("email")
    if not email:
        return contact

    db_path = get_gmail_db_path()
    if not db_path.exists():
        return contact

    try:
        conn = connect_read_only(db_path)
        try:
            thread_count, last_date = query_by_email(conn, email, limit=1)
        finally:
            conn.close()

        if thread_count:
            contact["email_frequency"] = thread_count
        if last_date:
            contact["last_contact_date"] = last_date

    except Exception as e:  # noqa: BLE001
        log.debug("Gmail cache query failed for %s: %s", email, e)

    return contact


def enrich_contact(contact: dict[str, Any], retry_count: int = 0) -> ContactRecord | None:
    """Enrich a single contact and validate it against the schema."""
    log.info("Enriching: %s (%s)", contact.get("full_name"), contact.get("account"))

    if not contact.get("email") and not contact.get("linkedin_url") and not contact.get("phone"):
        log.info("  No contact method - searching web...")
        contact = search_web_for_contact(contact)

    if not any([contact.get("email"), contact.get("linkedin_url"), contact.get("phone")]):
        log.info("  Failed: No contact method found for %s", contact.get("full_name"))
        return None

    if contact.get("email"):
        contact = query_gmail_cache(contact)

    contact["retry_count"] = retry_count

    if not contact.get("confidence"):
        try:
            temp_record = ContactRecord(
                full_name=contact.get("full_name", "Unknown"),
                company=contact.get("company", "Unknown"),
                account=contact.get("account", "unknown"),
                email=contact.get("email"),
                linkedin_url=contact.get("linkedin_url"),
                phone=contact.get("phone"),
                title=contact.get("title"),
                source=contact.get("source", "account-file"),
                last_contact_date=contact.get("last_contact_date"),
                email_frequency=contact.get("email_frequency"),
                sf_role=contact.get("sf_role"),
                confidence="LOW",  # Placeholder
                retry_count=retry_count,
            )
            contact["confidence"] = temp_record._calculate_confidence()
        except Exception as e:  # noqa: BLE001
            contact["confidence"] = "LOW"
            log.warning("Could not calculate confidence: %s", e)

    try:
        record = ContactRecord(**contact)
        log.info("  Validated: %s confidence", record.confidence)
        return record
    except Exception as e:  # noqa: BLE001
        log.info("  Validation failed: %s", e)
        return None


def write_to_memory(contact: ContactRecord) -> None:
    """Write contact to memory/personal/contacts/contact_<name>_<account>.md file."""
    memory_dir = contacts_memory_dir()

    filename = f"contact_{normalize_name_for_filename(contact.full_name)}_{contact.account}.md"
    filepath = memory_dir / filename

    content = f"""---
name: contact-{normalize_name_for_filename(contact.full_name)}-{contact.account}
description: Contact record for {contact.full_name} at {contact.company}
metadata:
  type: reference
---

# {contact.full_name} — {contact.company}

**Title:** {contact.title or "[Unknown]"}
**Account:** {contact.account}
**Email:** {contact.email or "[Unknown]"}
**LinkedIn:** {contact.linkedin_url or "[Unknown]"}
**Phone:** {contact.phone or "[Unknown]"}

## Salesforce Role

{contact.sf_role or "[Not in Salesforce]"}

## Engagement Signals

**Last Contact:** {contact.last_contact_date or "[Unknown]"}
**Email Threads:** {contact.email_frequency or 0}

## Source & Confidence

**Discovered from:** {contact.source}
**Confidence:** {contact.confidence}
**Last enriched:** {contact.enriched_at}
"""

    filepath.write_text(content, encoding="utf-8")
    log.info("  Wrote to %s", filepath)


def _eligible_contacts(batch: list[dict[str, Any]], user_email: str | None) -> list[tuple[dict[str, Any], int]]:
    """Return contacts that may be enriched and their current retry counts."""
    eligible: list[tuple[dict[str, Any], int]] = []
    for contact in batch:
        contact_email = contact.get("email")
        if _should_skip_contact(contact_email, user_email):
            log.debug("Skipping excluded contact: %s", contact_email)
            continue
        eligible.append((contact, contact.get("retry_count", 0)))
    return eligible


def _enrich_candidate(candidate: tuple[dict[str, Any], int]) -> ContactRecord | None:
    """Run the expensive enrichment portion for one candidate."""
    contact, retry_count = candidate
    return enrich_contact(contact, retry_count=retry_count)


def _enrich_candidates(candidates: list[tuple[dict[str, Any], int]]) -> list[ContactRecord | None]:
    """Enrich candidates concurrently while preserving their input order."""
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(BATCH_SIZE, len(candidates))) as executor:
        return list(executor.map(_enrich_candidate, candidates))


def _reconcile_contact(
    record: ContactRecord,
    domain_map: dict[str, str],
    internal_domains: frozenset[str],
) -> ContactRecord:
    """Apply account-domain and confidence policy to an enriched contact."""
    email_domain = (record.email or "").split("@", 1)[-1].lower() if record.email and "@" in record.email else ""
    if email_domain and email_domain in domain_map:
        return record.model_copy(update={"account": domain_map[email_domain]})
    if email_domain and email_domain not in internal_domains:
        return record.model_copy(update={"confidence": "LOW", "source": "inferred-context"})
    return record


def _record_failed_contact(contact: dict[str, Any], retry_count: int, failed: list[dict[str, Any]]) -> None:
    """Advance retry state for a failed contact or log terminal exhaustion."""
    if retry_count < MAX_RETRIES:
        contact["retry_count"] = retry_count + 1
        failed.append(contact)
    else:
        log.info("Max retries reached for %s - skipping", contact.get("full_name"))


def _prepare_gmail_cache(candidates: list[tuple[dict[str, Any], int]]) -> None:
    """Run optional Gmail migrations once before concurrent cache reads."""
    if not any(contact.get("email") for contact, _retry_count in candidates):
        return
    db_path = get_gmail_db_path()
    if not db_path.exists():
        return
    try:
        prepare_database(db_path)
    except Exception as exc:  # noqa: BLE001
        log.debug("Gmail cache preparation failed; continuing with read-only queries: %s", exc)


def enrich_batch(contacts: list[dict[str, Any]], start_idx: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Enrich a batch concurrently, then reconcile and persist results in input order."""
    batch = contacts[start_idx : start_idx + BATCH_SIZE]
    candidates = _eligible_contacts(batch, get_user_email())
    _prepare_gmail_cache(candidates)
    domain_map = build_domain_account_map()
    internal_domains: frozenset[str] = frozenset(d.lower() for d in get_internal_domains())
    records = _enrich_candidates(candidates)
    enriched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for (contact, retry_count), record in zip(candidates, records, strict=True):
        if record is not None:
            reconciled = _reconcile_contact(record, domain_map, internal_domains)
            enriched.append(reconciled.model_dump())
            write_to_memory(reconciled)
        else:
            _record_failed_contact(contact, retry_count, failed)

    return enriched, failed


def run_enrichment_pipeline(raw_contacts: list[dict[str, Any]]) -> tuple[int, int]:
    """Run the batch enrichment pipeline over *raw_contacts*.

    Loads/saves ``checkpoint.json`` and appends to ``contacts-enriched.json``
    incrementally (atomic writes after each batch). Returns
    ``(total_enriched, total_failed)``.
    """
    from fieldkit.enrich._helpers import load_checkpoint, save_checkpoint
    from fieldkit.enrich._io import CONTACTS_ENRICHED
    from fieldkit.enrich.schema import EnrichmentCheckpoint

    checkpoint = load_checkpoint()
    if checkpoint:
        log.info(
            "Resuming from checkpoint: %s (processed %d)",
            checkpoint.last_completed_account,
            checkpoint.total_processed,
        )
        start_idx = checkpoint.total_processed
    else:
        start_idx = 0

    enriched_file = enrich_dir() / CONTACTS_ENRICHED
    enriched_contacts: list[dict[str, Any]] = (
        json.loads(enriched_file.read_text(encoding="utf-8")) if enriched_file.exists() else []
    )

    total_contacts = len(raw_contacts)
    failed_contacts: list[dict[str, Any]] = []

    current_idx = start_idx
    while current_idx < total_contacts:
        batch_num = (current_idx // BATCH_SIZE) + 1
        log.info(
            "=== Batch %d (contacts %d-%d) ===",
            batch_num,
            current_idx + 1,
            min(current_idx + BATCH_SIZE, total_contacts),
        )

        enriched_batch, failed_batch = enrich_batch(raw_contacts, current_idx)
        enriched_contacts.extend(enriched_batch)
        failed_contacts.extend(failed_batch)

        current_account = raw_contacts[min(current_idx + BATCH_SIZE - 1, total_contacts - 1)]["account"]
        checkpoint = EnrichmentCheckpoint(
            last_completed_account=current_account,
            last_completed_contact_index=current_idx + BATCH_SIZE,
            total_processed=current_idx + BATCH_SIZE,
            total_enriched=len(enriched_contacts),
            total_failed=len(failed_contacts),
        )
        save_checkpoint(checkpoint)
        _write_json_atomic(enriched_file, enriched_contacts)

        current_idx += BATCH_SIZE

    if failed_contacts:
        log.info("=== Retrying %d failed contacts ===", len(failed_contacts))
        for contact in failed_contacts:
            retry_count = contact.get("retry_count", 0)
            time.sleep(min(2**retry_count, 16) + random.uniform(0, 1))
            record = enrich_contact(contact, retry_count=retry_count)

            if record:
                enriched_contacts.append(record.model_dump())
                write_to_memory(record)
            else:
                log.info("Max retries reached for %s - skipping", contact.get("full_name"))

        _write_json_atomic(enriched_file, enriched_contacts)

    total_failed = len([c for c in failed_contacts if c.get("retry_count", 0) >= MAX_RETRIES])
    log.info("=== Enrichment complete === total_enriched=%d total_failed=%d", len(enriched_contacts), total_failed)
    return len(enriched_contacts), total_failed
