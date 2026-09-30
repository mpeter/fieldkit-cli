# Google Workspace CLI catalog

`gws` is optional and is not installed by fieldkit. Use it only when the
executable is present, its current help exposes the required Google Workspace
method, and the operator has authorized the intended account and scope.

## Discover the installed interface

Start with `gws --help`, then inspect the exact service, resource, and method with
that leaf's `--help`. For generated request details, use
`gws schema SERVICE.RESOURCE.METHOD` without recursive reference expansion.
Some CLI versions can fail while resolving recursive schema references, so
`--resolve-refs` is not a required or approved discovery step in this workflow.

Treat a nonzero exit, crash, malformed schema, or missing field definition as a
discovery failure. Do not guess the request body or retry a mutation from memory;
leave the operation pending until the installed CLI can describe it.

The current CLI convention uses `--params` for path and query parameters,
`--json` for a JSON request body, and `--dry-run` for local validation where that
leaf advertises it. Confirm these flags on the exact leaf because generated
interfaces can change.

## Available service families

The installed CLI may expose Gmail, Drive, Docs, Sheets, Calendar, People,
Slides, Tasks, Forms, Chat, Meet, Keep, Classroom, Workspace events, and Apps
Script. Presence in top-level help does not prove the account has the necessary
scope or that every resource method is available.

Use fieldkit's Gmail cache commands for supported account-centric local queries.
Use `gws` only when the requested operation requires a live Google Workspace
resource and the operator accepts that boundary.

For task creation and completion, prefer the shipped `fieldkit gtask create` and
`fieldkit gtask complete` commands. They preview by default and require
`--confirm` for the external write. Direct Google Tasks insert, patch, and delete
calls are outside this catalog; the task-sync skill owns its bounded list reads
and reconciliation rules.

For Docs layout work, follow the bounded
[Google Docs layout workflow](../references/docs-layout-workflow.md).

## Read and write discipline

Before a write, read the exact resource and capture only the fields needed to
detect conflicts. Confirm the authenticated account, resource identifier,
destination, and proposed change. Resolve every ambiguous match; never select a
calendar, contact, file, recipient, task list, or message by result order.

Use a dry-run when the method supports it. After an authorized write,
read the affected resource back and compare the requested fields with the
approved values. For visual documents, export and inspect the artifact when API
state alone cannot prove layout.

Email and Chat sending, sharing, permission changes, contact edits, calendar
changes, and task mutations always require authorization for the exact content
and destination. Draft creation is a write but is not permission to send.

## Authentication

Use `gws auth status` for a credential-safe status check. If login is required,
show the intended service scope and have the operator approve or run
`gws auth login --services SERVICE`. Use `gws auth setup` only when OAuth client
configuration itself is absent and the operator intends to configure it.

Do not use credential export as a diagnostic, print tokens, or copy credentials
into logs. If authentication or required scopes remain unavailable, leave the
operation pending. Do not substitute another transport and call it equivalent.
