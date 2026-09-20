# CLI routes

## Slack

Use `slackcli`. Inspect the subcommand before an unfamiliar operation:

```bash
slackcli search --help
slackcli conversations --help
slackcli messages --help
```

Sending or mutating Slack content requires explicit user authorization and a
read-back or direct result check.

## GitHub

Use `gh` for issues, pull requests, releases, workflow state, API calls, and
ordinary public code search. Use Git for local history and changes. Use the
direct global `gh_grep` MCP only when a cross-repository code-pattern search is
materially better suited to grep.app's index; it is not a gateway group.

```bash
gh issue list
gh pr view <number>
gh search code '<query>'
gh api <endpoint>
```

## fieldkit

Use the installed `fieldkit` CLI for account-centric Salesforce, Gmail cache,
pursuit, watcher, and workflow operations. Discover current leaves with:

```bash
fieldkit commands --json
fieldkit <group> --help
```
