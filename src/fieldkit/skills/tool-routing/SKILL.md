---
name: tool-routing
description: >
  You need to call an external service such as Google Workspace, Backstory,
  Tavily, Brave, Slack, GitHub, Dataverse, or a browser and need the primary CLI
  or the narrow MCP-only exception. Routes CLI-first and records when no route
  exists. Trigger with "tool routing", "which tool for [task]", "tool for Gmail",
  "MCP routing", "which MCP server", or "right tool for [service]".
metadata:
  opencode/slash: "true"
  category: ops
---

## Objective
Route every external operation through an installed CLI when one has capability
parity. Use MCP only for the five narrowly defined exceptions below. Avoid gateway
setup, schema load, and retry work for CLI-covered tasks.


## Primary routes
| Task | Primary route | Details |
|---|---|---|
| Account-centric cached Gmail queries | `fieldkit gmail query ...` | `fieldkit gmail query --help` |
| Gmail, Drive, Docs, Sheets, Calendar, Contacts, Slides, Tasks, Forms, Chat, Apps Script | `gws <service> ...` | [ops/workspace-tool-catalog.md](ops/workspace-tool-catalog.md) |
| Browser automation with the logged-in Chrome session | `chrome-use` | [references/browser-cli.md](references/browser-cli.md) |
| Tavily search, extract, crawl, map, research | `tvly` | [references/web-search.md](references/web-search.md) |
| Slack | `slackcli` | [references/non-mcp-tools.md](references/non-mcp-tools.md) |
| GitHub | `gh` and Git | [references/non-mcp-tools.md](references/non-mcp-tools.md) |
| Public GitHub code-pattern search | direct global `gh_grep` MCP | [references/non-mcp-tools.md](references/non-mcp-tools.md) |
| Vault read/write/search/history | native files, `rg`, Git, `qmd` | [references/vault.md](references/vault.md) |
| Official library docs | project docs or web; `gh` for code examples | [references/developer-search.md](references/developer-search.md) |


## MCP exceptions
Use these only when the requested capability matches the rationale exactly:

- MCP-NECESSITY fieldkit-sales: Backstory and Product Pages are proprietary services with no installed CLI or public file/API route. See [references/fieldkit-sales.md](references/fieldkit-sales.md).
- MCP-NECESSITY fieldkit-dataverse: Rover, Snowflake, and authenticated Jira data are exposed through the registered Dataverse MCP service and no installed CLI. See [references/fieldkit-dataverse.md](references/fieldkit-dataverse.md).
- MCP-NECESSITY direct global `gh_grep`: grep.app provides cross-repository public code-pattern search that is materially different from ordinary GitHub operations and `gh search code`. See [references/non-mcp-tools.md](references/non-mcp-tools.md).
- MCP-NECESSITY direct global `brave_search`: Brave is retained only as an independent search index when Tavily results need a materially different source. See [references/web-search.md](references/web-search.md).
- MCP-NECESSITY direct global `context7`: Context7 is retained only for its curated library corpus when project or official web docs are insufficient. See [references/developer-search.md](references/developer-search.md).


## Decision tree
1. Identify the exact operation and whether it is read-only or writes remote state.
2. If `fieldkit`, `gws`, `chrome-use`, `tvly`, `slackcli`, `gh`, Git, native files,
   or `qmd` covers it, use that route.
3. If it matches one of the five MCP-NECESSITY statements, use the named direct
   global MCP or, for an application-specific service, check the gateway and use only
   that group.
4. If neither applies, state that the capability is unavailable. Do not invent an
   endpoint or revive a removed group.
5. For credential failures, follow [workflows/first-time-setup.md](workflows/first-time-setup.md).


## CLI examples
- Gmail draft: `gws gmail users drafts create`; label change: `gws gmail users messages modify`.
- Docs edit: inspect with `gws docs documents get`, mutate with `gws docs documents batchUpdate`, then read back.
- Calendar focus/OOO creation: inspect the schema, then `gws calendar events insert`; event update: `gws calendar events patch`, then read back.
- Browser: load `chrome-use skills get core --full`, then use `chrome-use` for navigation, actions, and assertions.
- Vault: use native files/`rg` for exact content and `qmd` for lexical, vector, or hybrid retrieval.


## Write safety
CLI-first changes transport, not authority. Obtain the required user authorization
before remote sends, shares, permission changes, event/contact edits, or other
side effects. After an authorized write, read the affected resource back through
the same CLI and report the stored result.


## MCP gateway check
Run this only after selecting an application-specific MCP exception:

```bash
systemctl --user status mcpjungle
mcpjungle list groups
```

If the selected group is absent or the gateway is down, stop with the relevant
setup step. Do not redirect the request to a different data source and call it parity.


## Success criteria
- The primary CLI is selected whenever it covers the requested capability.
- MCP use names one of the five necessity boundaries and its direct or gateway route.
- Unavailable operations, including retired vault graph queries, are reported as unavailable.
- Remote writes retain authorization, verification, and failure reporting.
