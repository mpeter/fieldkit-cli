---
last_reviewed: 2026-09-29
covers:
  - src/fieldkit/config/
  - src/fieldkit/commands/init/
  - src/fieldkit/commands/brief/
  - src/fieldkit/commands/pipeline/
  - src/fieldkit/pipeline/
  - src/fieldkit/driver/
  - src/fieldkit/companion/
  - src/fieldkit/shadowbot/
  - src/fieldkit/sf/quota.py
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
| `name` | No | None | Display name used by identity-aware local workflows |
| `email` | No | None | User email used to exclude self-authored records |
| `email_domain` | No | None | Organization domain used only when a command must derive the user email |
| `role` | No | None | User-provided role retained by interactive setup |
| `company` | No | None | User-provided organization retained by interactive setup |

Minimal initialization creates a generic workspace without identity values:

```console
fieldkit init --minimal ./fieldkit-workspace
```

## Optional integration keys

| Key | Required by | Purpose |
| --- | --- | --- |
| `sf_org_url` | Salesforce authentication | Base URL for the Salesforce organization you are authorized to access |
| `gmail_token` | Google commands, when overriding the default | Google OAuth token path; relative paths resolve from the current working directory |
| `gmail_db` | Gmail commands, when overriding the default | Local Gmail SQLite cache path; relative paths resolve from the current working directory |
| `llm_model` | AI-assisted workflows | Supported LiteLLM model identifier |
| `vertex_location` | Vertex AI workflows | Provider region |
| `mcp_gateway_url` | MCP-backed workflows | Base URL of a gateway you operate or are authorized to use |
| `mcp_endpoints.backstory` | Backstory health watcher | Full HTTP(S) MCP endpoint supplied by your service operator |
| `mcp_endpoints.calendar` | Calendar section in the assembled morning brief | Full HTTP(S) MCP endpoint supplied by your service operator |
| `mcp_endpoints.draft_queue` | Draft-queue watcher | Full HTTP(S) MCP endpoint supplied by your service operator |
| `github_repo` | GitHub-backed issue and web PR commands | Public or private GitHub repository in `owner/repo` form |
| `companion.tier` | Companion workflows | `read` (default), `propose`, or `act`; invalid values fail closed to `read` |
| `companion.act_allowlist` | Companion workflows at `act` tier | Complete argv entries; empty by default. Every argument, value, and token order must match. Shell-style quotes represent tokens only; no shell is executed. |

Workspace identity written by interactive setup lives separately at
`<fieldkit_home>/config/identity.yaml`. Its `territory` and
`salesforce_user_id` values are template metadata, not global integration
settings. Setup also records the supplied name, email, role, and company there
for workspace-owned templates.

Companion permission is not operator authorization or an operating-system
sandbox. `companion allowed` checks the configured gate without executing the
candidate command. `companion run` executes permitted commands and writes an
audit journal, including denied attempts. Default `companion feed` polling can
write a delivery cursor; `companion feed --json --all` avoids advancing it.
The gate admits feed inspection through its read-tier table only when `--all`
is an actual option, not an account value. Unknown feed options fail closed.
Quota reporting through the read-tier table rejects `--set`, `--period`,
`--data-root`, and unknown options. Selecting `--source sf` still performs
credentialed Salesforce reads; read permission does not mean offline execution.
Saved report selection requires `brief open --no-open` or
`pipeline open --no-open`; `--json` alone still launches a system viewer and is
not granted read-tier permission. Unknown report-selection options fail closed.
Approve the actual account/source scope and local or remote effects separately;
do not treat the `read` tier as a universal no-filesystem-write guarantee.

`fieldkit_home` is the required workspace-root key. If it is absent, commands
that need the workspace fail with exit `3`; run `fieldkit init` or
`fieldkit init --minimal PATH` to write a current configuration.

Installing an optional profile does not populate any of these keys or grant
service access. See [Integrations and profiles](../integrations.md).

The three `mcp_endpoints` values are independent, optional capabilities. fieldkit
does not derive private route names or append organization-specific paths. Supply
the complete endpoint exactly as provided by an operator you trust:

```yaml
mcp_endpoints:
  calendar: https://gateway.example.com/calendar/mcp
```

Endpoints must use HTTP or HTTPS and cannot contain embedded credentials, query
parameters, or fragments. Leave a capability absent when you do not have that
service; local aggregate workflows report it as not run. Explicitly selecting a
live `backstory-health` or `draft-queue` watcher without its required endpoint is
invalid configuration and exits `3`. Their dry runs do not require provider
access.

## Pipeline quota

```yaml
pipeline:
  quota:
    target: 5000000
    period: "2026-H2"
```

`target` is the configured numeric quota used by forecast and quota commands.
`period` selects a calendar half-year (`YYYY-H1` or `YYYY-H2`) or quarter
(`YYYY-Q1` through `YYYY-Q4`).

## Driver scheduling

```yaml
driver:
  max_concurrent: 1
```

`max_concurrent` controls how many eligible, pairwise-disjoint prompt sources a
single `fieldkit driver run` may execute. The supported range is `1` through
`4`; integer values are clamped to that range, and values that cannot be converted
to an integer fall back to `1`. When `FIELDKIT_DRIVER_SPEND_CAP` is set,
the driver limits the batch to one issue so every attempt has an unambiguous
spend boundary.

The driver accepts `WorkOrder: docs/work-orders/<name>.md`, `OpenSpec:
openspec/changes/<name>/`, and `Speckit: specs/<name>/` issue references. A work
order carries its versioned `edit_sites` and `done_checks` contracts in
frontmatter. OpenSpec and Speckit directories use `driver.yaml` for both. The
driver validates every exact anchor
against the frozen `origin/main` revision before scheduling and creates the
execution worktree from that same commit. Missing or ambiguous anchors remain
non-passing; prose fences and line numbers are never inferred as authority.

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
      - acme-corp.example.com
    keywords:
      - Acme Corp
```

The `accounts` mapping is keyed by a filesystem-safe account slug. Common
optional fields include Salesforce record or territory identifiers, Gmail search
keywords, team addresses, and watcher thresholds. Start with the file generated
by `fieldkit init`; use the relevant command's `--help` before adding an advanced
field.

Set `pursuit_coverage_threshold` on an account to require more active pursuits
in the pipeline review's Pursuit Coverage check. The default is `1`. This
setting is separate from Gmail's `blindspots_min_messages` threshold.

An absent `accounts.yaml` means no optional account overrides. When a transcript
has action items to classify, task preparation requires a readable, valid mapping
if the file is present: malformed YAML, an invalid selected account entry, or
invalid attendee lists stop preparation rather
than silently changing task ownership. Brief and pipeline reports read
`config/accounts.yaml` in their selected workspace, including an explicit
`--data-root`, without falling back to a different workspace. When these reports
consult account configuration, invalid or duplicate content stops generation with
an invalid-data error. Other optional
configuration consumers may warn and use empty overrides; that fallback does not
apply to task preparation or these report reads.

## Three-root boundary

Workspace and runtime-data roots must be absolute after `~` expansion. The optional
`fieldkit_root` override selects an application checkout and has the same requirement;
empty or invalid overrides are configuration errors. Skill discovery uses this
validated override when present and bundled package resources otherwise. Relative
paths passed to initialization are resolved before being stored in configuration.

| Content | Resolver | Ownership |
| --- | --- | --- |
| Bundled package assets | `importlib.resources` | Application; available without a checkout |
| Explicit or discovered source checkout | `get_fieldkit_root()` | Application source; unavailable in a checkout-free install without an override |
| Workspace | `get_fieldkit_home()` | User-authored and optionally versioned data |
| Runtime data | `get_fieldkit_data()` | Application-managed caches, credentials, logs, and state |

Back up the workspace and any required runtime state independently. A source
checkout is not a workspace, and reinstalling fieldkit does not delete either
configured data root.

## Validate a change

```console
fieldkit doctor
```

The general doctor checks Salesforce, the Gmail cache, Google OAuth, and
ShadowBot. Invalid or incomplete data detected by those checks exits `3`; an
authentication problem exits `2`. Changing unrelated configuration will not fix
an authentication failure.

A passing result does not validate every LLM, MCP, or driver setting. Follow the
affected workflow's documented diagnostics or non-writing preview before enabling
its writes; do not treat doctor as a universal configuration validator.
