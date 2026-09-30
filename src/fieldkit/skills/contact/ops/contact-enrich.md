# Contact enrichment

Discover contacts already represented in a configured fieldkit workspace,
validate available contact methods, and review coverage. This workflow writes
personal contact data locally. Confirm the workspace and intended account scope
before running it; do not use a repository checkout as the data destination.

## Discover local sources

`fieldkit contact enrich --discover` scans account stakeholder tables,
Salesforce-role information stored in pursuit frontmatter, and the existing
local Gmail cache. It writes `contact-enrich/contacts-raw.json` under the
configured fieldkit workspace. It does not sign in to Salesforce, refresh
Gmail, search the web, or generate a web-search batch. Missing or stale source
data remains missing or stale in the discovery result.

Use `--account acme-corp` to limit discovery to one fictional account slug and
`--json` when a machine-readable summary is needed. Inspect the reported source
counts and the local raw file before treating any contact as current. The
command writes even when it discovers zero contacts.

Account filtering does not isolate storage. `--discover --account acme-corp`
replaces the shared `contact-enrich/contacts-raw.json` and its
`.contacts-raw-prev.json` backup with that account's discovery result, including
an empty result. Preserve any raw contacts needed for other accounts before
changing discovery scope.

## Optional web results

Web research is separate and requires the operator's source and privacy
approval. If authorized results already exist, supply a JSON array in
`contact-enrich/web-search-results.json` under the configured workspace.
Each result is matched to a raw contact by `full_name` and `account`; supported
additions are `linkedin_url`, `email`, and `phone`. Do not include a value that
was not verified from its stated source.

`fieldkit contact enrich --apply-web` merges those fields into
`contacts-raw.json` only where the raw field is empty. It does not perform a
search. The command reports zero applied results when the file is absent or
empty. Review the merged file before running the full pipeline.

## Validate and report

`fieldkit contact enrich` processes the raw file and writes
`contact-enrich/contacts-enriched.json` plus generated records under
`memory/personal/contacts/` in the configured workspace. Each accepted
record needs a name, company, account, and at least one contact method.
Its `HIGH`, `MEDIUM`, or `LOW` tier reflects field completeness, not
independent verification of identity, relationship, or customer consent.
The command may consult an existing Gmail cache for engagement fields; it does
not refresh that cache. Use `--json` to inspect the processed and failed counts.

`fieldkit contact report` reads the local raw and enriched files and writes
`contact-enrich/report.md` when enriched contacts exist. The report may contain
names, contact methods, and account context. Review it privately before sharing
or copying any proposed update into an account record. An empty report result
does not prove there are no contacts; check discovery and validation outcomes.

`fieldkit contact report --account acme-corp` filters the report contents but
overwrites the same shared `contact-enrich/report.md` when enriched contacts
exist. Preserve a report needed for another account before generating it.

## State and recovery

The pipeline maintains `contact-enrich/checkpoint.json` for batch progress.
It is tied to the raw-contact ordering; changing discovery scope or raw data
before a retry can make an old checkpoint inapplicable. Inspect the checkpoint
and files before rerunning, and do not describe a rerun as idempotent without
checking its outputs. Keep generated contact files, reports, and cache data out
of commits and public diagnostics.

Enrichment writes contact records only to `memory/personal/contacts/`. Files
already in `contact-enrich/memory/` are left untouched; enrichment does not
move, delete, or merge them automatically.

For command flags and usage, use `fieldkit contact enrich --help` and
`fieldkit contact report --help` from the installed version. For contact lookup
and other operations, return to the [contact skill](../SKILL.md).
