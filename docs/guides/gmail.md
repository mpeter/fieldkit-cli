---
last_reviewed: 2026-09-13
covers:
  - src/fieldkit/commands/gmail/
  - src/fieldkit/gmail/
audience: ae-user
---

# Gmail Sync

## Prerequisites

- `fieldkit init` has been completed (run `fieldkit doctor` to confirm)
- Your Google account is authenticated (OAuth token exists — see first-time setup below)

## First-time OAuth setup

The first time you run any Gmail command, fieldkit needs permission to read your Gmail.
Run:

```
fieldkit gmail sync
```

If no OAuth token exists, fieldkit prints an authorization URL to the terminal. Open that
URL in your browser, grant access when prompted, and fieldkit stores the token to
`<fieldkit_data>/google-oauth-token.json`. You will not be asked again until the token
expires.

> **Note:** `fieldkit gmail sync` must be run in an interactive terminal for the initial
> OAuth flow. If run from a systemd timer, cron job, or any non-interactive context with
> no cached token, it exits with code 2 and a message directing you to run it manually
> first. Complete the browser consent flow once, and subsequent automated runs will use
> the cached token without prompting.

## Sync modes

```
fieldkit gmail sync
```

With no selection flag, fieldkit automatically chooses the safe mode from the local
database state. A complete database uses Gmail history for an incremental sync. A new or
incomplete database starts or resumes a full sync. A typical incremental sync takes
20–40 seconds depending on message volume.

To deliberately refetch the entire mailbox, run:

```
fieldkit gmail sync --full
```

This is a resumable upsert refresh. It starts at the first Gmail message page, preserves
its page checkpoint if interrupted, and reconciles mailbox changes that occur during the
scan before marking the database complete. It refreshes rows returned by Gmail; it does
not remove cached rows merely because they were absent from the full listing.

To backfill from a known UTC date without changing automatic sync continuity, run:

```
fieldkit gmail sync --since 2026-06-01
```

`--since` includes messages at UTC midnight on the selected date and resumes from its
own saved page checkpoint after interruption. It upserts matching messages and leaves
the full/incremental history state untouched. It cannot be combined with `--full`.

Progress and status output go to the log (not stdout). To see sync activity, check
your system logs or run with verbose logging enabled.

Optional flags:

- `--db PATH` — override the path to `gmail.db`
- `--full` — force a resumable full-mailbox refresh
- `--since YYYY-MM-DD` — run an isolated, resumable backfill on or after a UTC date
- `--max-messages N` — stop the selected resumable operation after a cumulative,
  nonnegative number of processed messages (`0` means unlimited). Retry with a larger
  value or `0` to continue from the saved page checkpoint.

## Apply intel after sync

After syncing, run these two commands to push Gmail signals into your workspace:

**Tag threads by account:**

```
fieldkit gmail account-tags
```

This scans your synced messages for Gmail labels matching the `ref/*` pattern
(e.g. `ref/acme-bank`) and maps each thread to its account in the database.
Labels must be configured in Gmail as filters — fieldkit does not read
`accounts.yaml` domain entries for this step.

Expected output:

```
Upserted 47 thread-account associations across 5 account(s):
  acme-bank: 23 threads
  globalpay: 14 threads
  ...
```

**Generate Gmail intelligence reports:**

```
fieldkit gmail enrich-pursuits
```

This reads your synced Gmail database and writes a `gmail-intel.md` report to each
configured account directory (`accounts/<account>/gmail-intel.md`). Each report
contains top contacts by email volume, champion signals, per-pursuit thread matches,
and a review checklist. It does **not** modify pursuit frontmatter files.

Optional flags:

- `--account SLUG` — generate the report for one account only

## Review blindspot contacts

Use `fieldkit gmail query blindspots ACCOUNT` to find active contacts in an
account's tagged threads. By default, the command sets aside addresses that match
a narrow masked-data signal: the address uses one of the account's configured
domains and its name-like local part ends in a dot-separated, four-character
lowercase ASCII letter/digit token containing at least one letter. Numeric endings
such as `.2026` remain ordinary. The command reports the number set aside; pass
`--include-suspected` to inspect them, visibly marked as `SUSPECTED`.

JSON output keeps actionable records in `items` and exposes set-aside records in
`suspected_items`, with `suspected_count` and a stable `quality_reason`. Accounts
without configured domains retain their existing results because fieldkit does not
guess a domain from the account slug. Generated `gmail-intel.md` reports apply the
same classification and state how many addresses were set aside.

## Check sync health

Verify your Gmail database is at the correct path and check when it was last modified:

```
stat <fieldkit_data>/gmail.db
```

where `<fieldkit_data>` is the path from `fieldkit_data` in your `config.yaml`
(defaults to `<fieldkit_home>/data`). If the date is more than 24 hours ago, run
`fieldkit gmail sync` to refresh.

## Common errors

**OAuth token expired or missing:**

Delete the token file and re-run sync in an interactive terminal. fieldkit will print an
authorization URL for you to open in a browser:

```
rm <fieldkit_data>/google-oauth-token.json
fieldkit gmail sync
```

If you are running sync on a schedule (systemd timer, cron) and see exit 2 with a "not a
TTY" message, run `fieldkit gmail sync` manually first to complete the consent flow, then
your automated runs will resume normally.

**Database not found:**

The database has not been created yet. Run:

```
fieldkit gmail sync
```

This will create the database from scratch. Subsequent runs will be incremental.

**Database integrity issue:**

```
fieldkit doctor gmail
```

Run this to check database health. If the database is corrupt, delete it and resync:

```
rm <fieldkit_data>/gmail.db
fieldkit gmail sync
```
