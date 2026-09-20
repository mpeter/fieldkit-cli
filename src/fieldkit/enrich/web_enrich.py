#!/usr/bin/env python3
"""Web enrichment orchestrator using the Tavily CLI.

Reads contacts marked with _needs_web_enrichment from contacts-raw.json,
outputs batch search requests to web_search_batch.json for an agent to process with `tvly`.

The agent should:
1. Read web_search_batch.json
2. For each contact, run `tvly search <query> --json`
3. Write results to web-search-results.json
4. Re-run enrich_contacts.py which will pick up the results
"""

import json
from typing import Any

from fieldkit.enrich._io import CONTACTS_RAW, WEB_SEARCH_RESULTS, enrich_dir


def extract_contacts_needing_enrichment() -> list[dict[str, Any]]:
    """Find contacts that need web enrichment."""
    raw_file = enrich_dir() / CONTACTS_RAW
    if not raw_file.exists():
        return []

    raw_data: list[dict[str, Any]] = json.loads(raw_file.read_text(encoding="utf-8"))

    # Filter to contacts without email/linkedin/phone
    needs_enrichment: list[dict[str, Any]] = []
    for contact in raw_data:
        if not any([contact.get("email"), contact.get("linkedin_url"), contact.get("phone")]):
            needs_enrichment.append(
                {
                    "full_name": contact.get("full_name"),
                    "company": contact.get("company"),
                    "account": contact.get("account"),
                    "title": contact.get("title"),
                    "search_query": f'"{contact.get("full_name")}" {contact.get("company")} LinkedIn email',
                }
            )

    return needs_enrichment


def main() -> None:
    """Generate batch search request file."""
    contacts = extract_contacts_needing_enrichment()

    if not contacts:
        print("No contacts need web enrichment.")
        return

    # Write batch request
    batch_file = enrich_dir() / "web_search_batch.json"
    batch_file.write_text(json.dumps(contacts, indent=2), encoding="utf-8")

    print(f"Generated batch search request for {len(contacts)} contacts")
    print(f"Output: {batch_file}")
    print("")
    print("Next step: process each query with `tvly search <query> --json`")
    print(f"and write results to {WEB_SEARCH_RESULTS}")


if __name__ == "__main__":
    main()
