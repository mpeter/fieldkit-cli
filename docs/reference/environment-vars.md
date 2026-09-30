---
last_reviewed: 2026-09-28
covers:
  - src/fieldkit/config/
  - src/fieldkit/llm/
  - src/fieldkit/commands/gmail/
audience: user
---

# Environment variables

Most durable settings belong in the [fieldkit configuration file](config-file.md),
normally `~/.config/fieldkit/config.yaml`. Environment variables are
appropriate for per-run overrides, CI isolation, and credentials supplied by a
secret manager. Ordinary child processes inherit exported variables. Gmail
authentication also loads the workspace `.env` and attempts dotenv discovery;
existing environment values take precedence over those files.

## User identity

| Variable | Purpose |
| --- | --- |
| `FIELDKIT_USER_EMAIL` | Explicit current-user email for email-derived workflows |
| `USER` | Shell username used with the configured email domain when FIELDKIT_USER_EMAIL is empty |

Prefer `FIELDKIT_USER_EMAIL` when email-derived workflows are enabled. Use a
fictional address in tests and examples.

The configured domain comes from `email_domain`, or from the domain portion of
`email` or `identity.email` when `email_domain` is unset. Without a username or
domain, fieldkit cannot derive an email address from `USER`.

## Google OAuth

| Variable | Fallback alias | Purpose |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | None | OAuth client identifier for Google consent |
| `GOOGLE_OAUTH_CLIENT_SECRET` | None | OAuth client secret |

A valid saved Google token, or one that can be refreshed, does not require
duplicate client settings in the environment. Set both client settings when
initial consent or reauthorization is needed, and complete consent in an
interactive terminal.

## AI and transcription

| Variable | Fallback alias | Purpose |
| --- | --- | --- |
| `FIELDKIT_LLM_MODEL` | `LLM_MODEL` | Supported LiteLLM model override |
| `FIELDKIT_ANTHROPIC_MODEL` | `ANTHROPIC_DEFAULT_SONNET_MODEL` | Vertex AI model name fallback after the general model environment variables |
| `FIELDKIT_NO_LLM` | None | Disable provider calls and select documented deterministic no-AI behavior |
| `FIELDKIT_TRANSCRIBE_MODEL` | None | Explicit transcription model; no provider is selected by default |
| `FIELDKIT_VERTEX_LOCATION` | `CLOUD_ML_REGION`, `VERTEX_LOCATION`, `GOOGLE_CLOUD_REGION` | Vertex AI region fallback after configured `vertex_location` |
| `VERTEXAI_PROJECT` | None | Explicit Vertex AI project read by LiteLLM |
| `GOOGLE_CLOUD_PROJECT` | None | Project used by Google application-default credential discovery when no explicit LiteLLM project is selected |
| `GOOGLE_APPLICATION_CREDENTIALS` | None | Standard Google credential-file path used by provider tooling |

The configured model must use a provider route supported by fieldkit. Model
access, billing, data handling, and region availability belong to the provider
and your organization. Any nonempty `FIELDKIT_NO_LLM` value disables provider
calls, including `0`; unset it to enable calls.

Model selection checks an explicit caller override, then `FIELDKIT_LLM_MODEL`,
`LLM_MODEL`, `FIELDKIT_ANTHROPIC_MODEL`, and `ANTHROPIC_DEFAULT_SONNET_MODEL`,
before configuration and the default. The Anthropic aliases supply a model name
that fieldkit prefixes with `vertex_ai/`.

Region selection checks configured `vertex_location` first, then the four
environment names in the order shown, and finally `us-east5`. Empty values and
`global` are skipped.

## Path overrides

| Variable | Purpose |
| --- | --- |
| `XDG_CONFIG_HOME` | Absolute base directory for fieldkit configuration and Salesforce cookie files; useful for isolated trials and CI |
| `XDG_CACHE_HOME` | Absolute cache base; the harness scratch root defaults to its `fieldkit/` child |
| `FIELDKIT_DATA_DIR` | Absolute runtime-data root override |
| `FIELDKIT_HARNESS_ROOT` | Absolute scratch root for disposable harness worktrees; overrides `XDG_CACHE_HOME` |
| `FIELDKIT_LLM_LOG` | Absolute LLM-call database path within an allowed fieldkit root |
| `FIELDKIT_SKILLS_DIR` | Skill directory override; relative paths resolve against the current working directory |
| `FIELDKIT_MCP_GATEWAY_URL` | MCP gateway base URL fallback when `mcp_gateway_url` is not configured |

Use absolute paths for predictable behavior across working directories.
`FIELDKIT_DATA_DIR` and `FIELDKIT_HARNESS_ROOT` require absolute roots;
`FIELDKIT_DATA_DIR` may select any absolute runtime-data root.
`FIELDKIT_LLM_LOG` remains restricted to its documented allowed roots.

The LLM log override must resolve beneath `~/.config/fieldkit`,
`~/.local/share/fieldkit`, the workspace, the active runtime-data root, or the
configured runtime-data root, when those configured roots are available.
Changing `XDG_CONFIG_HOME` does not by itself approve that directory as an LLM
log root. These checks resolve paths before comparing them with the allowed roots.

If neither `FIELDKIT_HARNESS_ROOT` nor an absolute `XDG_CACHE_HOME` is set,
fieldkit uses `~/.cache/fieldkit` for disposable harness worktrees. This cache is
separate from the application, workspace, and runtime-data roots.

## Driver limits

| Variable | Purpose |
| --- | --- |
| `FIELDKIT_DRIVER_SPEND_CAP` | Optional non-negative USD daily cap for driver LLM calls; invalid, unreadable, or reached caps deny the run, and any configured cap limits an admitted non-dry batch to one issue |
| `FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT` | Required positive daily run count for `fieldkit driver admit` |
| `FIELDKIT_DEVELOPER_SPEND_CAP` | Required non-negative USD cap for `fieldkit driver admit` |

The admission variables govern the separately invoked developer-job lease. The
driver spend cap governs a driver iteration itself. A missing required
admission value, invalid value, unreadable spend ledger, or exceeded cap denies
the operation rather than silently running without a limit.

## One command or one shell

Set a non-secret value for one invocation:

```console
FIELDKIT_NO_LLM=1 fieldkit brief generate --pipeline-only --dry-run
```

Export a value for subsequent commands in the current shell:

```console
export FIELDKIT_USER_EMAIL=user@example.com
fieldkit doctor
```

Do not place secrets in command-line arguments, shell history, committed dotenv
files, issue bodies, or CI logs. Use the operating system, CI platform, or
organization's approved secret store.

## Organization-provided adapters

Some optional adapters require additional variables or browser-session material
defined by the service operator. Those services are not part of fieldkit's
portable-core guarantee. Follow the administrator's private deployment guidance
and never publish session tokens while asking the fieldkit community for help.
