# Local data and privacy

fieldkit is a single-user local application. It has no hosted fieldkit service and no shared
multi-tenant database. Your operating-system account is the application boundary.

## What stays local

The workspace, generated Markdown, configuration, caches, logs, and runtime databases are stored in
the configured workspace, data, and user-configuration roots. None of these locations is part of the
Python package or source repository.

Treat these files as sensitive. Depending on the integrations you enable, they may contain customer
names, opportunity data, email content, meeting notes, prompts, and model responses. Do not commit
them to a public repository or attach them to an issue without redaction.

## What can leave the machine

Only commands using a configured external integration send requests beyond the workstation:

| Capability | Typical data destination |
| --- | --- |
| Salesforce | The Salesforce organization you configure |
| Google | Gmail, Drive, Docs, or other enabled Google APIs |
| LLM | The configured supported model provider |
| MCP-backed tools | The MCP gateway you configure, and the downstream services registered with it |
| Backstory | People.ai, through your MCP gateway's sales group; the `backstory-health` watcher sends configured account names and People.ai account identifiers |
| Organization-provided services | The service endpoint configured by your operator; for example, a ShadowBot prompt, thread identifier, and returned response |

Not every upstream address is yours to choose. `fieldkit auth backstory` registers People.ai's
fixed endpoint, `https://mcp.people.ai/mcp`, with your gateway; you choose the gateway, not that
upstream service.

With the `chrome-auth` dependencies installed (directly or through the `all` profile), ShadowBot commands can read your local Chrome cookie
store to recover an expired session. That read stays on the machine, but the recovered session is
then used against the configured ShadowBot service. See
[Connect an organization-provided assistant](guides/shadowbot-auth.md#enable-chrome-recovery-on-linux).

The base installation and minimal first-success workflow require none of these integrations after
the package is installed.

## Credentials

Credentials live outside the source tree. fieldkit uses provider-supported tokens, local credential
stores, or application-default credentials according to the selected integration. Never paste a
credential, session cookie, customer record, full email, or unredacted diagnostic bundle into a
public issue.

Use the repository security policy for suspected vulnerabilities. Use the support path for setup
questions that do not contain sensitive data.
