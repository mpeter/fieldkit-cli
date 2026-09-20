---
last_reviewed: 2026-09-10
covers:
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

Run diagnostics first, then generate to standard output:

```console
fieldkit doctor
fieldkit brief generate --dry-run
```

`--dry-run` prevents the brief file from being written. It does not force
configured data sources offline: a section may still contact its provider.

To limit the result to one configured account:

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

## Write the brief

Remove `--dry-run` after reviewing the preview:

```console
fieldkit brief generate
```

The default command writes a dated Markdown file below the configured
`<fieldkit_home>/briefs/` directory. Use `--json` when a caller needs the
generation result as machine-readable output.

## Interpret missing or degraded sections

- A not-yet-run watcher produces an empty-state message; run the named watcher
  if you want that section.
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
fieldkit brief generate --help
```

The generated [CLI reference](../cli-reference.md#fieldkit-brief-generate) is
authoritative for current option spelling.
