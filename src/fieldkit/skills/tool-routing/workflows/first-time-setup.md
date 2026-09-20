# First-time authentication

## Google Workspace CLI

```bash
gws auth status
gws auth login
```

Use `gws auth setup` only when OAuth client configuration itself is absent. After
login, retry a read-only operation for the required service before any authorized
write.

## Other CLIs

Use the CLI's own auth/status flow (`gh auth status`, `tvly auth`, or the relevant
`slackcli auth` command). Never print tokens or decrypted credentials into logs.

## MCP-only exceptions

First confirm that the capability matches one of the five exceptions. For
`fieldkit-sales` or `fieldkit-dataverse`, check the gateway and group registration:

```bash
systemctl --user status mcpjungle
mcpjungle list groups
```

For direct global `gh_grep`, `brave_search`, or `context7`, confirm that the client
loaded the named server; do not look for a mcpjungle group. Follow the managed
configuration's secret source for a missing route. Do not copy secrets into the
skill, shell history, issue, or PR.
