# Vault routes

Vault markdown is plain content under the workspace root. Use native file tools,
`rg`, and Git for exact reads, writes, search, and history. Use `qmd` for lexical,
vector, or hybrid retrieval:

```bash
qmd query "<question>"
qmd search "<exact terms>"
git log -- <path>
git diff -- <path>
```

Respect field ownership: pursuit frontmatter goes through
`fieldkit.pursuit.io`, and Salesforce-owned account fields go through the
corresponding `fieldkit sf` command.

There is no vault MCP backend. The former vault group was removed as phantom
configuration. Backlink,
outlink, orphan, and connection-path graph operations have no maintained route;
state that limitation instead of inventing an endpoint.
