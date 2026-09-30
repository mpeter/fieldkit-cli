---
last_reviewed: 2026-09-29
covers:
  - src/fieldkit/brief/
  - src/fieldkit/commands/brief/
  - src/fieldkit/watch/
audience: user
---

# Generate a morning brief

The morning brief combines available watcher output, meetings, and pipeline
context into Markdown. Its contents depend on the services and workspace data
you have configured; an optional section being unavailable does not imply that
the portable core is broken.

## Preview without writing a file

Run diagnostics first:

```console
fieldkit doctor
```

Then generate to standard output:

```console
fieldkit brief generate --dry-run
```

`--dry-run` prevents the brief file from being written and does not contact the
configured calendar or model provider. The rendered brief marks those inputs as
not run for the preview.

To scope account-specific pipeline and project-health inputs to one configured
account, use `--account`. Existing shared watcher snapshots and global
cross-account signals can still include other accounts:

```console
fieldkit brief generate --dry-run --account acme-corp
```

## Use local pipeline context only

When watcher aggregation or calendar access is not wanted, use the narrower
pipeline path. Add `--no-llm` to avoid model synthesis:

```console
fieldkit brief generate --pipeline-only --no-llm --dry-run
```

This is the safest way to inspect brief rendering from local pursuit, task, and
cached signal data. Whether a specific cache exists still depends on prior use.

The pipeline path requires a configured workspace, including with `--dry-run`.
If configuration is missing or invalid, it exits `3` before collecting data or
calling the model and directs you to `fieldkit init`. It never substitutes the
application directory for the workspace.

## Write the brief

Remove `--dry-run` after reviewing the preview:

```console
fieldkit brief generate
```

The default command writes a dated Markdown file below the configured
`<fieldkit_home>/briefs/` directory. Use `--json` when a caller needs the
generation result as machine-readable output.

Without `llm_model`, the command uses the local deterministic pipeline renderer.
When `mcp_endpoints.calendar` is absent, the calendar section says it was not
configured and independent local sections still render. Configuring either
capability explicitly selects its provider checks for a non-dry run.

## Interpret missing or degraded sections

- A disabled optional watcher with no corresponding cached output produces a
  not-run message. Existing cached output remains visible. Configure its stated
  endpoint, or select Slack explicitly, before running the named watcher.
- An unavailable configured calendar or other provider is reported as degraded
  while independent sections continue where possible.
- An authentication error needs user action and exits `2`.
- A partial run may exit `1`; inspect the named source and retry only when the
  failure is transient.

Use `fieldkit watch status`, `fieldkit watch logs --list`, and the relevant
`fieldkit doctor <service>` command before changing configuration.

## Other useful options

```console
fieldkit brief generate --date 2026-09-10 --dry-run
fieldkit brief generate --verbose --dry-run
```

List the current options without reading configuration or contacting a provider:

```console
fieldkit brief generate --help
```

The generated [CLI reference](../cli-reference.md#fieldkit-brief-generate) is
authoritative for current option spelling.

## Inspect a saved brief

Select the newest saved brief without launching a viewer:

```console
fieldkit brief open --no-open --json
```

The result identifies the selected file, its age, and `opened: false`. Pipeline
reviews have the same selection mode: `fieldkit pipeline open --no-open --json`.
These commands require a non-empty UTF-8 report of at most 4 MiB in the configured
workspace's `briefs/` directory; symlinked report files or that directory are
rejected with exit `3`.

Without `--no-open`, both commands request the system viewer, including with
`--json`. `opened: true` means the browser accepted the file URI, not that the
report rendered. A declined or failed launch returns `opened: false` and exits
`1`; open the selected file manually. File validation is not a sandbox against
another process replacing files after selection.
