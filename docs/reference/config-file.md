---
last_reviewed: 2026-09-10
covers:
  - src/fieldkit/config/
  - src/fieldkit/commands/init/
audience: user
---

# Configuration file

fieldkit's user configuration is YAML at:

```text
~/.config/fieldkit/config.yaml
```

When `XDG_CONFIG_HOME` is an absolute path, fieldkit instead uses
`$XDG_CONFIG_HOME/fieldkit/config.yaml`. The Salesforce cookie file follows the
same directory. Relative XDG paths are ignored, as required by the XDG base
directory specification.

Prefer `fieldkit init` to create or update it. Paths must be absolute where the
table says so. Keep credentials out of source control and use the provider's
documented token location or environment variable instead of inventing new keys.

## Core keys

| Key | Required | Default | Purpose |
| --- | --- | --- | --- |
| `fieldkit_home` | Yes for a configured workspace | None | Absolute path to user-owned workspace files |
| `fieldkit_data` | No | `<fieldkit_home>/data` | Absolute path to runtime databases, tokens, logs, and generated state |
| `name` | No | None | Display name written by interactive setup |
| `email` | No | None | User email used to exclude self-authored records |
| `email_domain` | No | None | Organization domain used only when a command must derive the user email |
| `role` | No | None | User-provided role label |
| `company` | No | None | User-provided organization label |

Minimal initialization creates a generic workspace without identity values:

```console
fieldkit init --minimal ./fieldkit-workspace
```

## Optional integration keys

| Key | Required by | Purpose |
| --- | --- | --- |
| `sf_org_url` | Salesforce authentication | Base URL for the Salesforce organization you are authorized to access |
| `salesforce_user_id` | User-scoped Salesforce workflows | Salesforce user identifier |
| `territory` | Territory-scoped Salesforce workflows | Organization-defined territory identifier |
| `gmail_token` | Google commands, when overriding the default | Absolute path to the Google OAuth token |
| `gmail_db` | Gmail commands, when overriding the default | Absolute path to the local Gmail SQLite cache |
| `gcp_project` | Vertex AI workflows | Provider project identifier |
| `llm_model` | AI-assisted workflows | Supported LiteLLM model identifier |
| `vertex_location` | Vertex AI workflows | Provider region |
| `mcp_gateway_url` | MCP-backed workflows | Base URL of a gateway you operate or are authorized to use |
| `github_repo` | `fieldkit issue` commands | Public or private GitHub repository in `owner/repo` form |

Installing an optional profile does not populate any of these keys or grant
service access. See [Integrations and profiles](../integrations.md).

## Pipeline quota

```yaml
pipeline:
  quota:
    target: 5000000
    period: "2026-H2"
```

`target` is the configured numeric quota used by forecast and quota commands.
`period` is a user-defined label shown with that target.

## Organization-provided assistant

The optional `shadowbot` adapter uses a nested section whose URLs and identifiers
must come from the service administrator:

```yaml
shadowbot:
  api_base: https://assistant.example.com/api
  token_endpoint: https://login.example.com/oauth/token
  auth_endpoint: https://login.example.com/oauth/authorize
  redirect_uri: https://assistant.example.com/oauth/callback
  client_id: example-client
  assistant_id: example-assistant
```

The open-source project does not operate this service or provide working values.
Do not copy sample endpoints into a real deployment.

`api_base` must be a public HTTPS URL with a path. fieldkit rejects local and
private literal addresses, non-default HTTPS ports, embedded credentials, query
parameters, and fragments before it sends an authorization bearer token.

## Account configuration

Account-specific settings live at
`<fieldkit_home>/config/accounts.yaml`, separate from global configuration:

```yaml
internal_domains:
  - example.com
gmail_label_prefix: ref/
accounts:
  acme-corp:
    domains:
      - acme-corp.com
    keywords:
      - Acme Corp
```

The `accounts` mapping is keyed by a filesystem-safe account slug. Common
optional fields include Salesforce record or territory identifiers, Gmail search
keywords, team addresses, and watcher thresholds. Start with the file generated
by `fieldkit init`; use the relevant command's `--help` before adding an advanced
field.

## Three-root boundary

| Content | Resolver | Ownership |
| --- | --- | --- |
| Installed package and assets | `get_fieldkit_root()` | Application; do not write runtime state here |
| Workspace | `get_fieldkit_home()` | User-authored and optionally versioned data |
| Runtime data | `get_fieldkit_data()` | Application-managed caches, credentials, logs, and state |

Back up the workspace and any required runtime state independently. A source
checkout is not a workspace, and reinstalling fieldkit does not delete either
configured data root.

## Validate a change

```console
fieldkit doctor
```

Invalid or incomplete configuration exits `3` and names the problem. An
authentication problem exits `2`; changing unrelated configuration will not fix
it.
