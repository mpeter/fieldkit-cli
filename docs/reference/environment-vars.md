---
last_reviewed: 2026-09-13
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
secret manager. A child process sees only variables exported into its own
environment.

## User identity

| Variable | Purpose |
| --- | --- |
| `FIELDKIT_USER_EMAIL` | Explicit user email for commands that must exclude self-authored messages or drafts |
| `USER` | Shell username; combined with configured `email_domain` only when no explicit email is available |

Prefer `FIELDKIT_USER_EMAIL` when email-derived workflows are enabled. Use a
fictional address in tests and examples.

## Google OAuth

| Variable | Fallback alias | Purpose |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | `GOOGLE_CLIENT_ID` | OAuth client identifier for Google consent |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `GOOGLE_CLIENT_SECRET` | OAuth client secret |

The primary name wins when both are set. These values are needed only when the
equivalent authorized configuration is not already available. Complete initial
consent in an interactive terminal.

## AI and transcription

| Variable | Fallback alias | Purpose |
| --- | --- | --- |
| `FIELDKIT_LLM_MODEL` | `LLM_MODEL` | Supported LiteLLM model override |
| `FIELDKIT_NO_LLM` | `NO_LLM` | Disable provider calls and select documented deterministic no-AI behavior |
| `FIELDKIT_TRANSCRIBE_MODEL` | `TRANSCRIBE_MODEL` | Explicit transcription model; no provider is selected by default |
| `FIELDKIT_VERTEX_LOCATION` | `CLOUD_ML_REGION`, `VERTEX_LOCATION`, `GOOGLE_CLOUD_REGION` | Vertex AI region override |
| `GOOGLE_CLOUD_PROJECT` | `VERTEXAI_PROJECT` | Provider project when application-default credentials do not supply one |
| `GOOGLE_APPLICATION_CREDENTIALS` | None | Standard Google credential-file path used by provider tooling |

The configured model must use a provider route supported by fieldkit. Model
access, billing, data handling, and region availability belong to the provider
and your organization. `FIELDKIT_NO_LLM=1` is the preferred spelling for tests
and offline checks.

## Path overrides

| Variable | Purpose |
| --- | --- |
| `XDG_CONFIG_HOME` | Absolute base directory for fieldkit configuration and Salesforce cookie files; useful for isolated trials and CI |
| `XDG_CACHE_HOME` | Absolute cache base; the harness scratch root defaults to its `fieldkit/` child |
| `FIELDKIT_DATA_DIR` | Absolute runtime-data root override |
| `FIELDKIT_HARNESS_ROOT` | Absolute scratch root for disposable harness worktrees; overrides `XDG_CACHE_HOME` |
| `FIELDKIT_LLM_LOG` | Absolute LLM-call database path within an allowed fieldkit root |
| `FIELDKIT_SKILLS_DIR` | Packaged-skill directory override for development and tests |
| `FIELDKIT_SF_PIPELINE_ROOT` | Salesforce pipeline root override |
| `FIELDKIT_MCP_GATEWAY_URL` | MCP gateway base URL override |

Overrides that define roots must be absolute. `FIELDKIT_DATA_DIR` may select any
absolute runtime-data root. `FIELDKIT_LLM_LOG` remains restricted to its
documented allowed roots.

If neither `FIELDKIT_HARNESS_ROOT` nor an absolute `XDG_CACHE_HOME` is set,
fieldkit uses `~/.cache/fieldkit` for disposable harness worktrees. This cache is
separate from the application, workspace, and runtime-data roots.

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
