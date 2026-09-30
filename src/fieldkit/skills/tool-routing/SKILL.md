---
name: tool-routing
description: >
  Choose a supported fieldkit, Google Workspace, file, GitHub, browser, Slack,
  or web route without assuming private services or uninstalled tools.
metadata:
  opencode/slash: "true"
  category: ops
---

# Choose a supported tool route

Use this skill to identify how an operation can be performed in the current
environment. A route is usable only when its executable or tool is present, its
documented interface covers the operation, and the required identity and
authorization are known. Discovery is not proof of authentication or permission.

## Start with shipped fieldkit behavior

Start with the installed `fieldkit` command registry for account, Gmail-cache,
pursuit, watcher, workflow, and supported integration operations. Run
`fieldkit commands --json` to inspect the current leaf commands, write classes,
and account scope. Then inspect the selected leaf's `--help` before constructing
arguments. In a source checkout, use the checkout's documented development
launcher so a separately installed command cannot answer for another revision.

Do not infer that a similarly named command exists.
Do not invent a command, endpoint, plugin, server, or compatibility route when
the registry lacks the capability. Report the operation as unavailable or
identify the documented setup that is missing.

## Select optional capabilities explicitly

Optional tools are capabilities, not fieldkit prerequisites:

- Google Workspace operations may use a separately installed and authenticated
  `gws` CLI. Follow the [Google Workspace catalog](ops/workspace-tool-catalog.md).
- Local workspace content uses native file reads, `rg`, and Git. Follow the
  [workspace content routes](references/vault.md).
- GitHub work may use Git and a separately installed `gh` CLI. Other public
  developer research starts with project and official documentation. Follow the
  [CLI routes](references/cli-routes.md) and
  [developer search routes](references/developer-search.md).
- Browser automation, Slack access, and web research require a tool the operator
  has separately installed and authorized. fieldkit does not guarantee a client,
  command name, credential store, or account. Follow the
  [web research boundary](references/web-search.md) and, for Slack, the
  [Slack search protocol](references/slack-search-protocol.md).

If more than one installed tool could perform the operation, prefer the one with
the narrowest documented scope and strongest preview or read-back support. Do not
silently switch data sources: different services are not equivalent evidence.

## Decide before acting

1. Name the exact read or write, target service, target account, and expected
   result.
2. Check the fieldkit registry first. If fieldkit owns the behavior, use its
   documented command and safety flags.
3. Otherwise, verify that an optional tool is installed and inspect its current
   help. Confirm authentication with a credential-safe status operation.
4. Resolve ambiguous accounts, resources, recipients, and destinations before a
   write. Never choose the first result by list order.
5. Preview when the supported interface offers a dry-run. Show the exact proposed
   mutation and obtain the required operator approval.
6. Run the bounded operation, preserve its exit status, and verify the result
   through an independent read or artifact inspection.
7. If any prerequisite or verification step fails, leave the operation pending.
   Follow the [failure scenarios](references/failure-scenarios.md) instead of
   claiming partial success.

Read-only access does not authorize a write. Authorization for one resource or
account does not authorize another. A successful transport response does not
prove the intended state was stored.

For first-time credential setup, use the
[authentication workflow](workflows/first-time-setup.md). Salesforce next-step
changes use the separate
[Salesforce next-step protocol](references/sf-next-steps-protocol.md).

## Report the outcome

Name the route selected, identity and scope checked, operation attempted, and
verification performed. Mark each requested operation as verified, pending,
failed, skipped, or unavailable. Never collapse missing credentials, an unknown
tool, incomplete pagination, ambiguous identity, or failed read-back into success.
