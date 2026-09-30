# fieldkit watcher contributor guide

Watchers share run-status reporting, alert deduplication, and persistence helpers.
Keep watcher-specific scanning and alert decisions in the relevant domain module.

## Status and persistence

Use `fieldkit.watch.status` to report and inspect outcomes rather than parsing
stdout. Read the reported `outcome` together with `records_checked` and `failures`.
The shared classifier reports `ok` only with zero failures, `partial` when some
records were checked and failures remain, and `fatal` when failures occurred
before any record was checked. Preserve `partial` and `fatal` outcomes through
callers rather than reporting unconditional success.

Run status lives in the configured workspace's `watchers` directory.
`write_run_status()` uses `locked_json_update()` and returns `written`, `skipped`
for a dry run, or `failed` when persistence raises `OSError`, `TypeError`, or
`ValueError`. Those failures produce a fixed warning rather than propagate.
Inspect the returned result when persistence is part of the watcher's success
contract; `failed` must remain nonpassing. This is a contributor requirement,
not proof that every existing caller already checks the result. A missing or
stale status entry is not proof of a successful current run.

Use each watcher's state helpers for alert-suppression state. The shared
`fieldkit.watch.state.merge_state()` applies a scan's delta under a lock so a
concurrent scan does not replace unrelated keys. Do not replace this with an
unlocked read-modify-write.

`fieldkit.watch.dedup.alert_block_exists()` checks for a case-sensitive heading
prefix. No matching heading means only that the helper found no previous block;
the watcher's eligibility checks still determine whether to produce an alert.

## Pursuit transition dates

`apply_detected_transition_date()` owns the pursuit-stall transition sentinel.
It bridges a detected stage change while frontmatter has not caught up, preserves
the detected date on subsequent unchanged-stage scans, and bounds its lifetime.
Do not reset the date on every scan: that would continually reset the stall age.
Stage changes, updated frontmatter, malformed dates, and expiration are handled
by that helper and its stage-change tests, not by a second implementation.

## Account selection

The Backstory health watcher skips accounts with `internal: true` in account
configuration. Preserve this filter when changing account selection; those
accounts are deliberately outside its external-signal checks.

## Paths and tests

Resolve watcher storage through the configured workspace helpers, not the
checkout or a maintainer's service layout. Tests patch helpers where the watcher
imports them. `get_watchers_dir()` participates in `clear_config_caches()`;
watcher-local cached path accessors must also join the autouse isolation fixture.

Use UTC dates for date-based status and deduplication decisions. A local-calendar
date can change those decisions across machines near midnight.

Scheduled processes need their own validated configuration and credentials.
Do not infer a service's environment or integration availability from an
interactive shell or from another contributor's machine.
