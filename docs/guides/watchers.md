---
last_reviewed: 2026-09-10
covers:
  - src/fieldkit/commands/watch/
  - src/fieldkit/watch/
audience: user
---

# Run watchers

Watchers inspect configured workspace or integration data and record conditions
that may need attention. Each watcher has its own prerequisites. You do not need
to configure every watcher, and organization-provided data sources are not part
of the portable-core guarantee.

## Discover available watchers

```console
fieldkit watch run --help
```

The current command lists watchers for pursuit stalls, close-date countdowns,
contract expiry, waiting-on items, draft queues, Slack threads, and optional
account-health data. Read a watcher's help before its first run:

```console
fieldkit watch run pursuit-stalls --help
```

## Preview a single watcher

Where a watcher supports `--dry-run`, use it before writing alert or state files:

```console
fieldkit watch run pursuit-stalls --dry-run
```

Then run without `--dry-run` when the reported scope is correct. Use only
watchers whose workspace fields and external services you have configured.

## Preview or run the configured set

```console
fieldkit watch run --all --dry-run
fieldkit watch run --all
```

The all-watchers command runs the configured sequence and generates a morning
brief. A once-per-day guard prevents an accidental duplicate successful run;
`--force` explicitly bypasses that guard. `--allow-partial` accepts a completed
partial pass but never hides a fatal failure.

## Inspect outcomes

```console
fieldkit watch status
fieldkit watch status --json
fieldkit watch logs --list
fieldkit watch logs pursuit-stalls --tail 100
```

Outcomes have distinct meanings:

- `ok`: all selected work completed;
- `partial`: some selected records failed, so inspect the log and decide whether
  retrying is safe; and
- `fatal`: the watcher could not perform meaningful work.

Expected exclusions, such as terminal pursuits or an account intentionally
marked internal, are not failures.

## Schedule only after a clean manual run

fieldkit can install a user crontab entry for the all-watchers command:

```console
fieldkit watch run --all --install-cron --cron-time '0 6 * * *'
```

Run the same command manually first, verify `fieldkit watch status`, and confirm
that the scheduler inherits the required environment and credential access.
Platform scheduling behavior is outside the portable-core support contract.

The generated [CLI reference](../cli-reference.md#fieldkit-watch) documents each
watcher and its exact options.
