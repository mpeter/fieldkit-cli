# MCP exception routes

MCP is available only for five capabilities that lack CLI parity:

| Route | Narrow purpose |
|---|---|
| `fieldkit-sales` | Proprietary Backstory and Product Pages data |
| `fieldkit-dataverse` | Rover, Snowflake, and authenticated Jira data |
| direct global `gh_grep` | Cross-repository public code-pattern search |
| direct global `brave_search` | Brave independent-index search after Tavily |
| direct global `context7` | Context7 curated library corpus |

Select the capability first. Check the gateway only for a `fieldkit-*` route:

```bash
systemctl --user status mcpjungle
mcpjungle list groups
```

A running gateway does not prove that a group is registered or authenticated.
For a direct global route, inspect the server loaded by the client instead.
If the required group is missing, stop with the relevant dependency. Do not use
MCP for Google Workspace, browser automation, Tavily, Slack, GitHub, public code
search already covered by `gh`, or ordinary vault work.
