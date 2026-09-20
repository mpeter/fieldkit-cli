# Contact Enrichment Skill

Autonomous contact enrichment pipeline that discovers stakeholders from multiple sources, enriches with web search and internal intelligence, validates against schema, and generates actionable integration recommendations.

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## When to Use

Trigger with:
- "enrich contacts"
- "contact enrichment"
- "refresh contact data"
- "discover new contacts"
- "update stakeholder intelligence"
- "run contact pipeline"

**Cadence:** Run monthly or before QBRs to refresh stakeholder data.

## What This Does

### Phase 1: Multi-Source Discovery

Scans all accounts and extracts contacts from:

1. **account.md stakeholder maps** — parses table format (Name | Title | SF Contact Role | Supplemental)
2. **Salesforce contact roles** — from pursuit frontmatter `sf_contact_roles` field
3. **Gmail cache** — `fieldkit sync` for email frequency and engagement

**Output:** `<data_root>/contact-enrich/contacts_raw.json` (discovered contacts with source attribution)

### Phase 2: Web Enrichment via `tvly`

For contacts missing email/LinkedIn/phone:

1. Generate search batch → `web_search_batch.json`
2. Process with `tvly search` in bounded batches of 10
3. Extract LinkedIn URLs and emails from search results
4. Write → `web_search_results.json`
5. Merge back into `contacts_raw.json`

**Success rate:** ~70% of contacts get LinkedIn URLs via web search

### Phase 2b: Dataverse Rover Enrichment (optional — internal contacts only)

For each contact on your organization's domain, if your directory integration is available:

1. Query Rover People data product for: title, manager, namespace, location
2. Add to contact record as:
   - `rover_title` — official job title from Rover
   - `rover_manager` — manager's full name from Rover
   - `directory_namespace` — organization username or namespace
3. If `fieldkit-dataverse` is unavailable or contact not found in Rover: skip and leave fields absent (do not fail enrichment for external contacts).

This step runs in parallel with web enrichment. Contacts outside the configured internal domains skip Rover entirely.

### Phase 3: Validation & Enrichment

Process contacts through Pydantic schema validation:

- **Required:** `full_name`, `company`, `account`, at least one contact method
- **Auto-calculated:** confidence tier (HIGH/MEDIUM/LOW)
- **Enrichment:** Gmail cache for engagement signals (if available)

**Output:** 
- `<data_root>/contact-enrich/contacts_enriched.json` (validated contacts)
- `<data_root>/contact-enrich/memory/contact_<name>_<account>.md` (individual contact files)

### Phase 4: Reporting & Integration

Generates:

1. **Coverage report** — enrichment rates, confidence distribution, failed contacts
2. **Integration recommendations** — LinkedIn URLs to add to account.md, high-engagement contacts for pursuits, SF sync gaps

## Workflow

### Full Pipeline (Automated)

```bash
# Step 1: Discover contacts and generate web search batch
fieldkit contact enrich --discover
# generates <data_root>/contact-enrich/web_search_batch.json
# and prints next steps for Claude to process via the tvly CLI
```

**Then process the bounded web-search batch with the CLI:**

```bash
tvly search "<full-name> <company> LinkedIn" --json
```

Collect the first matching profile URL for each contact and write the existing
result schema (`full_name`, `account`, `linkedin_url`, `email`) to the batch
output file. Keep each batch at ten contacts so rate and failure handling stay
bounded. Write the collected records to
`<data_root>/contact-enrich/web_search_results.json`.

**Then complete the pipeline:**

```bash
# Apply web results + validate + report
fieldkit contact enrich --apply-web
fieldkit contact enrich
fieldkit contact report
```

### Quick Run (Discovery Only)

```bash
# Just discover and report current state
fieldkit contact enrich --discover
fieldkit contact report
```

## Output Files

All output files live under `<data_root>/contact-enrich/` (configured via `fieldkit init`).

| File | Purpose |
|------|---------|
| `contacts_raw.json` | All discovered contacts with source attribution |
| `contacts_enriched.json` | Validated, enriched contacts meeting schema |
| `web_search_batch.json` | Contacts needing web enrichment |
| `web_search_results.json` | Web search results from the `tvly` CLI |
| `report.md` | Coverage report with enrichment rates and gaps |
| `checkpoint.json` | Resume point for interrupted runs |
| `memory/contact_*.md` | Individual contact files |

## Integration Points

### With /brief's update op

Contact enrichment can run as part of the daily `/brief` update workflow (`ops/update.md`):

```bash
# In brief's update op
fieldkit contact enrich --discover
# Check if new contacts found → flag for user
```

### With QBR Prep

Before QBRs, refresh contact data:

```bash
# Run discovery
fieldkit contact enrich --discover
# Check stakeholder completeness for the account
```

### With Stakeholder-Map Skill

Contact enrichment feeds stakeholder map updates. After enrichment completes, review the
coverage report for LinkedIn URLs and engagement signals to add to account.md:

```bash
fieldkit contact report
```

## Schema

Defined in `fieldkit/enrich/schema.py`:

```python
class ContactRecord(BaseModel):
    # Required
    full_name: str
    company: str
    account: str
    
    # At least one required
    email: Optional[str]
    linkedin_url: Optional[str]
    phone: Optional[str]
    
    # Optional
    title: Optional[str]
    sf_role: Optional[str]
    last_contact_date: Optional[str]  # ISO 8601
    email_frequency: Optional[int]
    
    # Metadata (auto-populated)
    source: ContactSource  # backstory | gmail | sf | account-file | web
    confidence: ConfidenceTier  # HIGH | MEDIUM | LOW (auto-calculated)
    enriched_at: str
    retry_count: int
```

**Confidence calculation:**
- **HIGH (6+ points):** email (2) + LinkedIn (2) + phone (1) + activity (1) + title (1) + SF role (1)
- **MEDIUM (3-5 points):** email OR LinkedIn + some metadata
- **LOW (0-2 points):** minimal data

## Current Limitations

1. **Gmail cache integration incomplete** — `people.py` doesn't output JSON, so email frequency and last contact dates aren't populated. Workaround: manually parse text output or add JSON support.

2. **Backstory discovery not implemented** — discovery phase doesn't call `get_recent_account_activity` to find contacts mentioned in activity logs. Workaround: manually run Backstory before enrichment.

3. **LinkedIn profile extraction** — currently gets URL only, not full profile (bio, location, experience). Enhancement: use `tavily_extract` for full profile data.

## Testing

```bash
# Run schema validation tests
uv run pytest tests/ -k contact -v
```

## Resumability

Pipeline supports interruption and resumption via `checkpoint.json`:
- Tracks last completed account and contact index
- Safe to Ctrl+C and restart
- Enrichment resumes from last checkpoint

## Success Metrics

From recent run (2026-05-14):
- 53 contacts discovered
- 37 enriched (69.8% success)
- 29 LinkedIn URLs found via web search (66% hit rate)
- 52 memory files created
- 0 validation errors

## See Also

- **Discovery playbook:** `playbooks/discovery.md` (question banks for call prep)
- **Gmail cache:** `fieldkit/gmail_cache/` (email intelligence pipeline)
- **Stakeholder map:** `meeting` skill's `ops/stakeholder-map.md` (building/updating buying committee maps)
- **Contact lookup:** `ops/contact-lookup.md` in this skill (unified contact profiles)

## Files

All enrichment commands are invoked via `fieldkit contact <subcommand>`. The underlying
code lives in `src/fieldkit/contact/` in this checkout.
