---
last_reviewed: 2026-09-13
covers:
  - src/fieldkit/__main__.py
  - src/fieldkit/commands/
audience: user
---

# What fieldkit does

fieldkit turns a directory of inspectable Markdown, YAML, JSON, and SQLite files
into a command-line workspace for account work. The local workspace is useful on
its own. External systems add context but are not prerequisites for installing,
opening, or validating it.

## Start with local workflows

Initialize a workspace and inspect available commands:

```console
fieldkit init --minimal ./fieldkit-workspace
fieldkit --help
fieldkit skill list
```

After interactive setup, fieldkit can scaffold pursuit files, audit structured
frontmatter, rank pipeline health, forecast configured pursuits, and manage local
tasks. Use `--help` on a command before applying a write to real data.

## Add context deliberately

Each external capability has two separate steps:

1. install its optional dependency profile; and
2. configure credentials and endpoints you are authorized to use.

Salesforce commands can read configured CRM records and synchronize selected
fields into local pursuit files. Google commands can maintain a local Gmail
cache and derive account context. AI-assisted commands route to the provider you
configure. Some commands target organization-provided services that the fieldkit
project does not operate or grant access to.

For qualified Salesforce opportunities, fieldkit reads the native ClosePlan
scorecard. Historical local MEDDPICC data is preserved as `legacy_meddpicc`, but
it is not treated as current qualification. Until a native stage policy is
ratified, advances that formerly depended on a score report `pending` and require
an explicit override reason.

```console
fieldkit pursuit audit
fieldkit sf meddpicc <opportunity-id> --json
fieldkit pursuit advance <account>/<pursuit> --dry-run
```

See [Integrations and profiles](integrations.md) for the boundary of each
capability. Installing fieldkit never creates an account with a provider.

## Daily workflow example

A configured user might:

```console
fieldkit doctor
fieldkit gmail sync
fieldkit sync
fieldkit pursuit health
fieldkit brief generate --dry-run
```

Only run the integration commands you have installed and configured. The dry-run
brief prints its result instead of writing a brief file; individual data sources
can still contact their configured providers.

## Understand generated state

fieldkit separates three persistent roots and one disposable cache root:

- **Application:** the installed executable and packaged resources.
- **Workspace (`fieldkit_home`):** files you intentionally edit or version,
  including pursuits, account notes, and tasks.
- **Runtime data (`fieldkit_data`):** caches, tokens, logs, generated output, and
  watcher state managed by fieldkit.
- **Harness scratch:** disposable source worktrees under
  `FIELDKIT_HARNESS_ROOT`, `$XDG_CACHE_HOME/fieldkit`, or
  `~/.cache/fieldkit`.

Reinstalling the application does not delete your workspace. Back up the
workspace and any runtime state you need independently. Never commit credentials,
customer data, mail content, generated diagnostics, or harness scratch content
to the fieldkit source repository.

## Degraded operation

fieldkit distinguishes missing optional configuration from a broken core. Local
commands continue when an unrelated integration is absent. Commands that require
an unavailable service return an actionable error or use a documented cached or
no-AI path where one exists.

Exit codes are stable automation signals: `0` means success, `1` means a partial
result that may be retryable, `2` means authentication needs user action, and `3`
means invalid or incomplete data/configuration. See [Exit codes](reference/exit-codes.md).

## Choose the next guide

- [Create or configure a workspace](guides/init.md)
- [Connect Salesforce](guides/salesforce-auth.md)
- [Connect Gmail](guides/gmail.md)
- [Generate a morning brief](guides/morning-brief.md)
- [Run watchers](guides/watchers.md)
- [Work with pipeline data](guides/pipeline-workflow.md)

The generated [CLI reference](cli-reference.md) is authoritative for command and
option spelling.
