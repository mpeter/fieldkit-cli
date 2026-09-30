---
last_reviewed: 2026-09-27
covers:
  - pyproject.toml
  - src/fieldkit/commands/auth/
  - src/fieldkit/commands/doctor/
  - src/fieldkit/config/_integrations.py
  - src/fieldkit/gmail/auth.py
audience: user
---

# Integrations and profiles

fieldkit's portable core has no mandatory SaaS dependency. Optional profiles add
client libraries for capabilities you choose; configuration and authorization
remain separate, explicit steps.

| Profile | Adds | Configuration or access still required |
| --- | --- | --- |
| base | Local CLI, workspace, pursuit, task, and skill behavior | None for minimal first success |
| `google` | Google API clients and OAuth support | Your OAuth client and consent for the APIs you enable |
| `llm` | LiteLLM and supported provider clients | Provider project/account, credentials, region, and model access |
| `web` | FastAPI and Uvicorn | Local web configuration; no hosted fieldkit service exists |
| `chrome-auth` | Browser credential-store dependencies | Supported Linux desktop, local browser profile, and authorized session |
| `all` | Every dependency above | Every service is still configured independently |

Install a chosen profile, for example, with `uv tool install 'fieldkit-cli[google]'`.
If the base tool is already installed, add `--force` to replace its tool
environment. In a source checkout, `make bootstrap` installs the locked
contributor environment with all extras.

## Salesforce

Salesforce support uses a session from an organization you are already authorized
to access. It does not provide a Salesforce account or bypass your organization's
login and API policy. Follow [Connect Salesforce](guides/salesforce-auth.md), then
verify with:

```console
fieldkit doctor sf
```

Some organizations restrict API query mechanisms or expose custom objects that
fieldkit cannot assume. Treat organization-specific behavior as deployment
configuration, not as a portable-core guarantee.

## Google and Gmail

`fieldkit meeting list` reads locally recorded workbook links and works without
the Google profile or provider access. Meeting group help is also base-capable.
The link, note, and open commands retain their Google profile requirement;
installing it does not configure or authorize the separate workbook service.

The `google` profile provides Gmail and Google API clients. You supply an OAuth
client and complete the first consent flow interactively. That flow requests one
shared scope set: Gmail read-only, Google Docs read/write, and Google Drive
read/write. It does not select narrower scopes for each command. Review the
requested access before granting consent, even if you only plan to sync Gmail.

The token defaults to `google-oauth-token.json` in the runtime-data root. A
configured `gmail_token` path overrides that location; keep it outside the source
repository and protect it as a credential.

See [Connect Gmail](guides/gmail.md) and verify configured credentials with:

```console
fieldkit doctor google
fieldkit doctor gmail
```

## AI-assisted workflows

The `llm` profile provides the client libraries, not model access. Configure a
supported provider and review what a command will send before using sensitive
workspace content. Set `FIELDKIT_NO_LLM=1` when exercising a documented no-AI
path.

The assembled `fieldkit brief generate` command uses its deterministic local
renderer when `llm_model` is absent or `FIELDKIT_NO_LLM=1` is set.
Only an explicitly configured model selects provider preflight and synthesis.

Model availability, data handling, retention, cost, and regional controls belong
to the provider and your organization. fieldkit does not make those decisions for
you.

## Organization-provided services

Commands such as the `shadowbot` and some MCP-backed workflows are adapters for
services an operator or organization may provide. The open-source project does
not host those services, issue credentials, or promise that their APIs are
available to the public. These commands are optional and do not affect portable
core health.

Only configure an endpoint and credential you are authorized to use. If your
organization does not provide the service, leave that capability unconfigured.

Backstory health, the assembled brief's calendar section, and draft-queue
monitoring each use an independent full URL under `mcp_endpoints`. fieldkit does
not ship a default route for them. Their absence does not block local pursuit,
ingest, watcher, sync, or no-LLM brief work. See the
[configuration reference](reference/config-file.md#optional-integration-keys).
Configured endpoints must complete the MCP initialization lifecycle and return
matching, schema-valid JSON-RPC responses. Malformed calendar, Backstory, or
draft data is reported as unavailable or failed; it is never treated as a
healthy empty result.

## Trust boundary

Installing a profile can contact package indexes to download dependencies; it
does not authorize access to your service accounts. A configured command can send
account identifiers, email-derived context, prompts, or other selected data to
its provider. Read [Local data and privacy](privacy.md) before using real data,
and use the relevant `fieldkit doctor` subcommand to distinguish missing
configuration from an authentication failure.
