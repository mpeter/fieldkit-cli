# Contact enrichment

Discover contacts from configured account files, pursuit Salesforce contact roles,
and the local Gmail cache; merge reviewed web results; then validate contact records
and generate a coverage report. Run this workflow when refreshing stakeholder data
before a meeting or account review.

Read the [tool routing contract](../../tool-routing/SKILL.md) before using external
sources. Use only configured, authorized routes. Confirm before changing an account
file or Salesforce; this workflow produces local enrichment and memory files.

## Discover and review

Run discovery against the configured workspace:

```bash
fieldkit contact enrich --discover --account acme-corp
```

The command writes `contacts-raw.json` and reports contact counts by source.
Omit `--account` to scan all configured accounts. Discovery reads existing Gmail
cache data; refresh that cache with `fieldkit gmail sync` when needed.

Discovery does not perform web searches or query Backstory. Review contacts missing
a contact method, then use an authorized web-search route to find evidence for their
email, LinkedIn URL, or phone. Do not infer a match from the name alone.

## Apply reviewed web results

Write a JSON array to `<fieldkit_home>/contact-enrich/web-search-results.json`.
Match each record to a discovered contact using `full_name` and `account`:

```json
[
  {
    "full_name": "Alex Example",
    "account": "acme-corp",
    "email": "alex@example.com"
  }
]
```

Records may also include `linkedin_url` and `phone`. Applying results fills missing
contact methods without replacing existing values. Use the same account scope
throughout this workflow:

```bash
fieldkit contact enrich --apply-web --account acme-corp
fieldkit contact enrich --account acme-corp
fieldkit contact report --account acme-corp
```

Enrichment validates records, writes enriched contacts and personal-memory files,
and reports successful and failed counts. Changing the account scope or raw
contacts restarts processing with a warning while retaining existing enriched
contacts and memory files. You no longer need to remove `checkpoint.json` when
changing scope. Restarted passes append results, so retained contacts may appear
more than once in the enriched output.

The report command writes `report.md` with coverage by account, source and
confidence tier, the most-engaged contacts, failed contacts that need manual
review, data-quality notes, Salesforce contact roles and a metric
reconciliation. Review it before authorizing changes to account files or CRM.
Every command above also supports `--json`.

## Storage

`<fieldkit_home>` is the configured workspace root, separate from installed code
and the runtime-data root. The current output locations are:

| Output | Location |
| --- | --- |
| Discovered contacts | `<fieldkit_home>/contact-enrich/contacts-raw.json` |
| Enriched contacts | `<fieldkit_home>/contact-enrich/contacts-enriched.json` |
| Reviewed web results | `<fieldkit_home>/contact-enrich/web-search-results.json` |
| Coverage report | `<fieldkit_home>/contact-enrich/report.md` |
| Progress checkpoint | `<fieldkit_home>/contact-enrich/checkpoint.json` |
| Contact memory | `<fieldkit_home>/memory/personal/contacts/contact_<name>_<account>.md` |

## Validation and recovery

An enriched contact requires `full_name`, `company`, `account`, `source`, and at
least one of `email`, `linkedin_url`, or `phone`. The pipeline calculates confidence
from contact methods, activity, title, and Salesforce role. Gmail engagement
signals are populated from the local database when an email and matching cache
data are available.

Enrichment saves a versioned checkpoint after each batch output is written. It
binds the processed index to the account filter and a SHA-256 fingerprint of every
raw-contact field in list order; dictionary key order does not affect the hash.
The same scope and input resume from the saved offset. Changed fields, order,
scope, legacy checkpoints without identity, unsupported versions, or out-of-range
offsets restart from the first contact with a warning. Existing output is retained.
Completed matching runs do no additional work; empty inputs leave checkpoint and
enriched data unchanged. Inspect failures and coverage rather than assuming every
discovered contact was enriched.

The canonical schema and persistence helpers live in `src/fieldkit/enrich/`;
discovery, web-result merging, enrichment orchestration, and reporting live in
`src/fieldkit/contact/`.

## Related workflows

- [Contact lookup](contact-lookup.md)
- [Stakeholder mapping](https://github.com/mpeter/fieldkit-cli/blob/main/src/fieldkit/skills/meeting/ops/stakeholder-map.md)
- [Gmail setup and sync](https://github.com/mpeter/fieldkit-cli/blob/main/docs/guides/gmail.md)
