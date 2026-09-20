"""fieldkit.contact.report — contact enrichment coverage report.

Click-free move of ``commands/enrich/generate_report.py``, backing
``fieldkit contact report``. Analyzes ``contacts-raw.json`` and
``contacts-enriched.json`` and produces a markdown coverage report.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from fieldkit.enrich._io import enrich_dir, load_enriched_contacts, load_raw_contacts
from fieldkit.provenance import derived_doc_banner, derived_doc_marker


def calculate_enrichment_rate(raw: list[dict[str, Any]], enriched: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate enrichment success rate."""
    total_raw = len(raw)
    total_enriched = len(enriched)

    rate = (total_enriched / total_raw * 100) if total_raw > 0 else 0

    return {
        "total_raw": total_raw,
        "total_enriched": total_enriched,
        "rate_percent": round(rate, 1),
        "failed": total_raw - total_enriched,
    }


def analyze_by_account(enriched: list[dict[str, Any]], raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Break down enrichment by account.

    enrich_contact() remaps record.account to a canonical slug (e.g.
    "Acme Corp" → "acme-corp") during enrichment, so the account keys in
    contacts-raw.json and contacts-enriched.json may differ. Iterating only
    over raw_by_account keys would silently drop enriched counts that landed
    under a slug with no matching raw entry, producing >100% rates and
    negative failed counts.

    Fix: union the key sets from both counters so every account that appears
    in either file gets a row. Clamp failed to >= 0 and cap rate at 100% to
    guard against any residual key-mismatch edge cases.
    """
    accounts: dict[str, dict[str, Any]] = {}

    raw_by_account = Counter(c.get("account", "unknown") for c in raw)
    enriched_by_account = Counter(c.get("account", "unknown") for c in enriched)

    all_accounts = set(raw_by_account) | set(enriched_by_account)

    for account in all_accounts:
        raw_count = raw_by_account.get(account, 0)
        enriched_count = enriched_by_account.get(account, 0)
        rate = min((enriched_count / raw_count * 100) if raw_count > 0 else 0, 100.0)
        failed = max(raw_count - enriched_count, 0)

        accounts[account] = {
            "raw": raw_count,
            "enriched": enriched_count,
            "rate": round(rate, 1),
            "failed": failed,
        }

    return accounts


def analyze_sources(enriched: list[dict[str, Any]]) -> dict[str, int]:
    """Count contacts by source."""
    return dict(Counter(c.get("source", "unknown") for c in enriched))


def analyze_confidence(enriched: list[dict[str, Any]]) -> dict[str, int]:
    """Count contacts by confidence tier."""
    return dict(Counter(c.get("confidence", "UNKNOWN") for c in enriched))


def top_engaged_contacts(enriched: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Get top N most-engaged contacts by email frequency."""
    contacts_with_freq = [c for c in enriched if (c.get("email_frequency") or 0) > 0]
    sorted_contacts = sorted(contacts_with_freq, key=lambda c: c.get("email_frequency") or 0, reverse=True)
    return sorted_contacts[:limit]


def find_failed_contacts(raw: list[dict[str, Any]], enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find contacts that failed enrichment.

    historic regression fix: enrich_contact() remaps record.account to a canonical slug
    (e.g. "Acme Corp" → "acme-corp") during enrichment, so the account field
    in contacts-raw.json and contacts-enriched.json may differ. Using
    (full_name.lower(), account) as the match key caused every contact whose
    account was remapped to be incorrectly classified as failed.

    Fix: use full_name.lower() as the primary match key, which is stable
    across slug remapping. When two contacts share the same full_name (rare
    but possible), fall back to email as a secondary discriminator if
    available — a contact is considered enriched if any enriched record
    matches on full_name AND (email matches OR neither side has an email).
    """
    enriched_by_name: dict[str, set[str]] = {}
    for c in enriched:
        name_key = c.get("full_name", "").lower()
        email = (c.get("email") or "").lower()
        enriched_by_name.setdefault(name_key, set()).add(email)

    failed = []
    for contact in raw:
        name_key = contact.get("full_name", "").lower()
        if name_key not in enriched_by_name:
            failed.append(contact)
            continue

        raw_email = (contact.get("email") or "").lower()
        enriched_emails = enriched_by_name[name_key]

        if raw_email and raw_email not in enriched_emails:
            failed.append(contact)
        # else: raw contact has no email — any enriched record with this name counts, not failed.

    return failed


def _coverage_pct(items: list[dict[str, Any]], field: str) -> str:
    """Return 'N / total (X%)' coverage string for a field."""
    total = len(items)
    count = sum(1 for c in items if c.get(field))
    pct = round(count / total * 100, 1) if total else 0
    return f"{count} / {total} contacts have {field} ({pct}%)"


def _section_overall(overall: dict[str, Any]) -> list[str]:
    return [
        "## Overall Enrichment Rate",
        "",
        f"- **Total contacts discovered:** {overall['total_raw']}",
        f"- **Successfully enriched:** {overall['total_enriched']}",
        f"- **Failed (no contact method found):** {overall['failed']}",
        f"- **Success rate:** {overall['rate_percent']}%",
        "",
        "---",
        "",
    ]


def _section_by_account(by_account: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## Enrichment by Account",
        "",
        "| Account | Raw | Enriched | Failed | Rate |",
        "|---------|-----|----------|--------|------|",
    ]
    for account, stats in sorted(by_account.items(), key=lambda x: -x[1]["enriched"]):
        lines.append(f"| {account} | {stats['raw']} | {stats['enriched']} | {stats['failed']} | {stats['rate']}% |")
    lines += ["", "---", ""]
    return lines


def _section_sources(sources_dist: dict[str, int]) -> list[str]:
    lines = ["## Source Distribution", "", "Where contacts were originally discovered:", ""]
    for source, count in sorted(sources_dist.items(), key=lambda x: -x[1]):
        lines.append(f"- **{source}:** {count} contacts")
    lines += ["", "---", ""]
    return lines


def _section_confidence(confidence: dict[str, int]) -> list[str]:
    return [
        "## Confidence Tier Distribution",
        "",
        "| Tier | Count | Criteria |",
        "|------|-------|----------|",
        f"| 🟢 HIGH | {confidence.get('HIGH', 0)} | Email + LinkedIn + recent activity + title/role |",
        f"| 🟡 MEDIUM | {confidence.get('MEDIUM', 0)} | Email or LinkedIn + some metadata |",
        f"| 🔴 LOW | {confidence.get('LOW', 0)} | Minimal data (name + company only) |",
        "",
        "---",
        "",
    ]


def _section_top_engaged(top_engaged: list[dict[str, Any]]) -> list[str]:
    if not top_engaged:
        return []
    lines = [
        "## Top 10 Most-Engaged Contacts",
        "",
        "By email thread count from Gmail cache:",
        "",
        "| Name | Account | Title | Email Threads | Last Contact |",
        "|------|---------|-------|---------------|--------------|",
    ]
    for contact in top_engaged:
        lines.append(
            f"| {contact.get('full_name', 'Unknown')} | {contact.get('account', 'unknown')}"
            f" | {contact.get('title', '—')} | {contact.get('email_frequency', 0)}"
            f" | {contact.get('last_contact_date', '—')} |"
        )
    lines += ["", "---", ""]
    return lines


def _section_failed(failed: list[dict[str, Any]]) -> list[str]:
    if not failed:
        return []
    lines = [
        "## Failed Contacts (Manual Review Required)",
        "",
        f"**{len(failed)} contacts** could not be enriched (no email, LinkedIn, or phone found after web search).",
        "",
        "| Name | Account | Title | Sources |",
        "|------|---------|-------|---------|",
    ]
    for contact in failed[:20]:
        sources: str = ", ".join(contact.get("_all_sources", ["unknown"]))
        lines.append(
            f"| {contact.get('full_name', 'Unknown')} | {contact.get('account', 'unknown')}"
            f" | {contact.get('title', '—')} | {sources} |"
        )
    if len(failed) > 20:
        lines += ["", f"*...and {len(failed) - 20} more. See contacts-raw.json for full list.*"]
    lines += ["", "---", ""]
    return lines


def _section_data_quality(enriched: list[dict[str, Any]]) -> list[str]:
    return [
        "## Data Quality Notes",
        "",
        "**Email coverage:**",
        f"- {_coverage_pct(enriched, 'email')}",
        "",
        "**LinkedIn coverage:**",
        f"- {_coverage_pct(enriched, 'linkedin_url')}",
        "",
        "**Title coverage:**",
        f"- {_coverage_pct(enriched, 'title')}",
        "",
        "**Engagement data:**",
        f"- {sum(1 for c in enriched if c.get('email_frequency'))} / {len(enriched)} contacts have email frequency data",
        f"- {sum(1 for c in enriched if c.get('last_contact_date'))} / {len(enriched)} contacts have last contact date",
        "",
        "---",
        "",
    ]


def _section_sf_roles(enriched: list[dict[str, Any]]) -> list[str]:
    sf_role_contacts = [c for c in enriched if c.get("sf_role")]
    if not sf_role_contacts:
        return []
    sf_roles = Counter(c.get("sf_role") for c in sf_role_contacts)
    lines = ["## Salesforce Contact Role Distribution", "", "| Role | Count |", "|------|-------|"]
    for role, count in sorted(sf_roles.items(), key=lambda x: -x[1]):
        lines.append(f"| {role} | {count} |")
    lines += ["", "---", ""]
    return lines


def _section_metric_reconciliation(enriched: list[dict[str, Any]], enrich_root: Path) -> list[str]:
    """Surface cache count vs pipeline count with explanation when they differ."""
    cache_count = len(enriched)
    checkpoint_path = enrich_root / "checkpoint.json"

    lines = [
        "## Metric Reconciliation",
        "",
        f"- **Cache count** (`contacts-enriched.json`): {cache_count} contacts",
    ]

    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        pipeline_count = data.get("total_enriched", None)
        if pipeline_count is None:
            lines.append("- **Pipeline count** (`checkpoint.json`): `total_enriched` field not found")
        else:
            lines.append(f"- **Pipeline count** (`checkpoint.json`): {pipeline_count} contacts")
            if cache_count != pipeline_count:
                lines += [
                    "",
                    "> **Count mismatch detected.** The cache count reflects contacts written to",
                    "> `contacts-enriched.json` at report time, while the pipeline count reflects the",
                    "> cumulative `total_enriched` value from `checkpoint.json` which persists across",
                    "> resumed pipeline runs. This difference is normal when the pipeline was resumed",
                    "> from a checkpoint — some contacts counted by the pipeline may have been",
                    "> deduplicated or superseded during the final write.",
                ]
    except FileNotFoundError:
        lines.append("- **Pipeline count** (`checkpoint.json`): not available (no checkpoint found)")

    lines += ["", "---", ""]
    return lines


def generate_report(enriched: list[dict[str, Any]], raw: list[dict[str, Any]]) -> str:
    """Generate markdown coverage report."""
    marker = derived_doc_marker(
        caste="summary",
        derived_from=["contacts-raw.json", "contacts-enriched.json"],
        generated_by="fieldkit contact report",
    )
    sections: list[str] = [
        marker.rstrip("\n"),
        "",
        "# Contact Enrichment Coverage Report",
        "",
        derived_doc_banner(),
        "",
        f"**Generated:** {datetime.now().isoformat()}",
        "",
        "---",
        "",
    ]

    sections += _section_overall(calculate_enrichment_rate(raw, enriched))
    sections += _section_metric_reconciliation(enriched, enrich_dir())
    sections += _section_by_account(analyze_by_account(enriched, raw))
    sections += _section_sources(analyze_sources(enriched))
    sections += _section_confidence(analyze_confidence(enriched))
    sections += _section_top_engaged(top_engaged_contacts(enriched, limit=10))
    sections += _section_failed(find_failed_contacts(raw, enriched))
    sections += _section_data_quality(enriched)
    sections += _section_sf_roles(enriched)

    return "\n".join(sections)


def build_report(*, account: str | None = None) -> str | None:
    """Load enriched/raw contacts, generate the report, and write ``report.md``.

    Args:
        account: Restrict the report to a single account slug. ``None``
            reports across every account.

    Returns:
        The generated markdown report, or ``None`` if there are no enriched
        contacts to report on (caller should point the user at
        ``fieldkit contact enrich`` first).
    """
    enriched = load_enriched_contacts()
    raw = load_raw_contacts()

    if account is not None:
        enriched = [c for c in enriched if c.get("account") == account]
        raw = [c for c in raw if c.get("account") == account]

    if not enriched:
        return None

    report = generate_report(enriched, raw)

    report_file = enrich_dir() / "report.md"
    report_file.write_text(report, encoding="utf-8")

    return report
