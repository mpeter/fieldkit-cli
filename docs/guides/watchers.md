---
last_reviewed: 2026-09-28
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

This preview does not write pursuit-stall alerts or watcher state. A direct
optional-provider preview, such as `backstory-health --dry-run`, also does not
read credentials or contact the provider; it reports that provider input was not
run. A direct Slack preview still reads Slack and only suppresses runtime writes;
use an aggregate preview for offline inspection. Individual local watcher help
remains authoritative for any diagnostic-log effects.

The thread watcher requires a separately installed and authenticated `slackcli`.
Its tested search-output contract is
[`slackcli 0.13.0`](https://github.com/shaharia-lab/slackcli/blob/0f81d64270239616a185c4f6e5d3f57116ebeeb1/src/commands/search.ts):
JSON contains a nonnegative integer `total` and a list of message mappings in
`matches`; a successful zero-match search can return empty stdout instead.
Failed processes and malformed JSON remain nonpassing. Other client versions
are unverified, and installing a fieldkit profile neither installs this client
nor grants Slack access.

Then run without `--dry-run` when the reported scope is correct. Use only
watchers whose workspace fields and external services you have configured.

Backstory health requires at least one finite engagement score from `0` through
`100` for each checked account. An empty or malformed opportunity result is a
provider-data failure: fieldkit writes neither a fictional zero-score alert nor
health state for that account. A mixed run can be partial; a run with no
successfully processed accounts is fatal.

`fieldkit watch run draft-queue --account SLUG` reports the scoped count in its
run outcome, but does not replace `draft-queue-alerts.md`; that file is the
all-account daily snapshot used by the morning brief. Run draft queue without
`--account` to update the snapshot.

## Preview or run the configured set

```console
fieldkit watch run --all --dry-run
```

After reviewing the preview, run the configured set or explicitly add Slack:

```console
fieldkit watch run --all
fieldkit watch run --all --slack
```

The aggregate command always runs the local waiting, pursuit-stall,
close-date, and contract-expiry watchers. It adds Backstory and draft-queue only
when their explicit endpoints are configured, and adds Slack only with
`--slack`. Its summary lists every optional input that was not run. It then
generates a morning brief, using local no-LLM rendering unless a model is
explicitly configured. `--dry-run` performs no credential preflight or provider
request. When Slack is selected for an aggregate preview, the summary explicitly
reports that its provider scan was not run; this is not a completed Slack scan.

A once-per-day guard suppresses duplicate live aggregate dispatch after selected
credential preflight succeeds. A prior same-day `ok` returns success; a prior
`partial`, `fatal`, or unrecognized outcome returns `1` without new work or
status publication. `--allow-partial` does not accept a suppressed run. Use
`--force` for a fresh live attempt; dry runs bypass the guard.

`--allow-partial` accepts only a live completed partial pass whose failed steps
completed and successfully published their status during that invocation. The
aggregate must also successfully publish its own `partial` status. Fatal,
interrupted, authentication, data, and persistence failures remain nonpassing.
Partial previews remain nonpassing, and an allowed pass is still recorded as
`partial`, not `ok`.

## Inspect outcomes

```console
fieldkit watch status
fieldkit watch status --json
fieldkit watch logs --list
fieldkit watch logs pursuit-stalls --tail 100
```

Outcomes have distinct meanings:

- `ok`: all selected records completed without a recorded failure;
- `partial`: some selected records failed, so inspect the log and decide whether
  retrying is safe; and
- `fatal`: the requested work could not complete safely, or required alert,
  state, report, or status publication failed.

Expected exclusions, such as terminal pursuits or an account intentionally
marked internal, are not failures.

## Schedule only after a clean manual run

Preview the exact user crontab entry without reading or changing the host
crontab:

```console
fieldkit watch run --all --install-cron --cron-time '0 6 * * *' --dry-run
```

After a clean manual `fieldkit watch run --all`, install that reviewed entry:

```console
fieldkit watch run --all --install-cron --cron-time '0 6 * * *'
```

Verify `fieldkit watch status`, and confirm that the scheduler inherits the
required environment and credential access.
Platform scheduling behavior is outside the portable-core support contract.

The generated [CLI reference](../cli-reference.md#fieldkit-watch) documents each
watcher and its exact options.
