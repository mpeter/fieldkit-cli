# Google Workspace CLI catalog

Use the installed `gws` CLI for Google Workspace. It exposes the generated API
surface, prints JSON by default, and does not require mcpjungle.

## Discover the exact command

```bash
gws <service> --help
gws <service> <resource> --help
gws schema <service.resource.method> --resolve-refs
```

Use `--params '<JSON>'` for URL/query parameters and `--json '<JSON>'` for a
request body. Use `--dry-run` for local request validation where available.
Do not guess fields: inspect the method schema first.

## Services

| Task | CLI route |
|---|---|
| Gmail messages, threads, attachments, drafts, labels, history, settings | `gws gmail users ...` |
| Drive files, folders, comments, permissions, revisions | `gws drive ...` |
| Docs get/create/batch update | `gws docs documents ...` |
| Sheets metadata, values, formatting, tables | `gws sheets spreadsheets ...` |
| Calendar lists, calendars, events, free/busy | `gws calendar ...` |
| Contacts, directory people, contact groups | `gws people ...` |
| Slides get/create/batch update | `gws slides presentations ...` |
| Task lists and tasks | `gws tasks ...` |
| Forms and responses | `gws forms forms ...` |
| Chat spaces, members, messages | `gws chat ...` |
| Apps Script projects and executions | `gws script ...` |

## Common command shapes

```bash
# Gmail: use fieldkit first for account-centric cached queries
fieldkit gmail query account <account>
gws gmail users messages list --params '{"userId":"me","q":"from:<sender> newer_than:7d"}'
gws gmail users messages get --params '{"userId":"me","id":"<message-id>"}'
gws gmail users drafts create --params '{"userId":"me"}' --json '<draft-resource-json>'
gws gmail users messages modify --params '{"userId":"me","id":"<message-id>"}' --json '<label-change-json>'

# Drive and Docs
gws drive files list --params '<search-params-json>'
gws drive permissions create --params '<permission-params-json>' --json '<permission-json>'
gws docs documents get --params '{"documentId":"<document-id>"}'
gws docs documents batchUpdate --params '{"documentId":"<document-id>"}' --json '<requests-json>'

# Sheets
gws sheets spreadsheets get --params '{"spreadsheetId":"<spreadsheet-id>"}'
gws sheets spreadsheets values get --params '<range-params-json>'
gws sheets spreadsheets values update --params '<range-params-json>' --json '<values-json>'

# Calendar and Contacts
gws calendar events list --params '<calendar-query-json>'
gws calendar events insert --params '<calendar-params-json>' --json '<event-json>'
gws calendar events patch --params '<event-params-json>' --json '<event-patch-json>'
gws calendar freebusy query --json '<freebusy-json>'
gws people people searchContacts --params '<search-params-json>'
gws people people updateContact --params '<contact-params-json>' --json '<person-json>'

# Other Workspace services
gws slides presentations batchUpdate --params '<presentation-params-json>' --json '<requests-json>'
gws tasks tasks insert --params '<task-list-params-json>' --json '<task-json>'
gws forms forms get --params '<form-params-json>'
gws chat spaces messages create --params '<space-params-json>' --json '<message-json>'
gws script projects getContent --params '<project-params-json>'
```

## Workflows

For every authorized write: read the resource, issue the mutation, then read it
again and verify the requested fields. When a search returns multiple plausible
files, calendars, contacts, or recipients, resolve the identity before writing.
Sending Gmail or Chat messages always requires explicit authorization.

Google Tasks uses the operator's `fieldkit` task list and the task-sync contract.
Resolve the list ID with `gws tasks tasklists list`; never guess it.

## Authentication

```bash
gws auth status
gws auth login
```

If scopes are missing, re-authenticate for the required service. Do not fall back
to a Workspace MCP group.
