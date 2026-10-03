---
name: tool-routing
description: Route external service tasks through installed CLIs or configured authorized MCP capabilities, checking availability, identity, and write authority before use.
metadata:
  opencode/slash: "true"
  category: ops
---

# Tool routing

Use an installed CLI when it covers the requested operation. For MCP capabilities,
read the active harness configuration and discover the configured authorized route
for this workspace. Documentation and configuration declarations do not prove
that a group is registered, a tool is loaded, or upstream authentication works.
Load only the schemas needed for the selected capability.

## Primary routes

| Task | Route |
|---|---|
| Salesforce and account-centric cached Gmail | `fieldkit sf ...` / `fieldkit gmail query ...` |
| Google Workspace services | `gws <service> ...`; see [CLI catalog](ops/workspace-tool-catalog.md) |
| Logged-in browser automation | `chrome-use`; see [browser CLI](references/browser-cli.md) |
| Web search, extraction, crawl, map, research | `tvly`; see [web search](references/web-search.md) |
| Slack / GitHub | `slackcli` / `gh` and Git; see [CLI routes](references/non-mcp-tools.md) |
| Vault content and agent history | native files, `rg`, Git, `qmd` / `ctx`; see [vault routes](references/vault.md) |
| Library documentation and code examples | project or official docs, `gh`; see [developer search](references/developer-search.md) for specialized capabilities |
| Proprietary account intelligence or enterprise data | configured authorized MCP capability, when available; see [account intelligence](references/account-intelligence.md) and [enterprise data](references/enterprise-data.md) |

## Select a capability

1. Identify the exact operation, data source, workspace, identity, and whether it
   reads or writes remote state.
2. Prefer the primary CLI when installed and capable. Inspect command help or API
   schemas before unfamiliar operations.
3. If CLI parity is absent, inspect the active harness configuration and exposed
   tools for a configured authorized MCP route. Verify the selected tool's scope,
   read/write policy, identity, and availability; do not assume a fixed group name
   or a direct server connection. See [route discovery](references/server-options.md).
4. If no authorized route covers the operation, report the missing capability.
   Do not revive retired aliases, borrow another workspace's registration, or
   substitute a different data source and claim equivalence.
5. Report authentication failures without switching identity or transport to bypass
   them. Follow [setup guidance](workflows/first-time-setup.md).

## Identity and writes

Verify the authenticated account before using `gws`; `userId=me` alone does not
establish identity. Pin browser actions to the authorized profile. Use the identity
approved for the task; do not substitute a personal account or another workspace.

Read-only MCP access does not authorize writes. Draft creation, sends, shares,
permission changes, document edits, and event/contact/task mutations require an
appropriate write-capable route and existing user authorization. Do not widen a
read-only group to obtain write access. After an authorized write, read back the
affected resource and report the stored result. Creating a draft does not authorize
sending it.

## Common operations

- Gmail draft: `gws gmail users drafts create`; label: `gws gmail users messages modify`.
- Docs: read with `gws docs documents get`, edit with `gws docs documents batchUpdate`, then read back.
- Calendar: inspect the method schema, create with `gws calendar events insert` or update with `gws calendar events patch`, then read back.
- Browser: load `chrome-use skills get core --full` before unfamiliar operations; verify saved state.
- Backstory: use account-level evidence only; Salesforce remains the deal system of record. See [account intelligence](references/account-intelligence.md).

## Missing capability

Check only the selected configured route. A running gateway does not prove upstream
access. Report a missing tool, registration, credential, or approved identity with
the relevant setup dependency. Do not reset OAuth or run bulk registration from a
task session. Offer another source only as an explicitly labeled alternative when
it answers the user's question. Retired vault graph operations have no maintained
route; see [failure scenarios](references/failure-scenarios.md).
