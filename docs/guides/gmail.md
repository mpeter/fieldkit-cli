---
last_reviewed: 2026-09-27
covers:
  - src/fieldkit/commands/gmail/
  - src/fieldkit/gmail/
  - src/fieldkit/config/_integrations.py
audience: ae-user
---

# Gmail Sync

## Prerequisites

- `fieldkit init` has been completed (run `fieldkit doctor` to confirm)
- Your Google account is authenticated (OAuth token exists — see first-time setup below)

Provider sync requires the `google` installation profile; see
[installation and first success](../getting-started.md). Local cache queries and `import-cache` do not
require that profile or contact Google.

## First-time OAuth setup

Before the first sync, configure `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET` for your Google OAuth application. The shared consent
flow requests Gmail read-only plus Google Docs and Google Drive read/write access,
including when started for Gmail alone. Review these permissions before granting
access; fieldkit does not select scopes per command. From an interactive terminal,
run:

```
fieldkit gmail sync
```

If no OAuth token exists, fieldkit prints an authorization URL to the terminal. Open that
URL in your browser, grant access when prompted, and fieldkit stores the token at
the configured `gmail_token` path, defaulting to
`<fieldkit_data>/google-oauth-token.json`. A valid saved token is reused, and an expired
token with a refresh token is renewed automatically. These saved-token paths do not
require duplicate OAuth client settings in the environment. Revoked or unusable
credentials require authorization again.

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
incomplete database starts or resumes a full sync. Duration depends on mailbox
volume, provider response times, and rate limits.

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

By default, progress and status go to the log rather than stdout. Pass `--json`
to print the sync outcome on stdout, including counts and retry status. To see
ongoing sync activity, check your system logs or run with verbose logging enabled.

Optional flags:

- `--db PATH` — override the path to `gmail.db`
- `--full` — force a resumable full-mailbox refresh
- `--since YYYY-MM-DD` — run an isolated, resumable backfill on or after a UTC date
- `--json` — print the final sync outcome as JSON on stdout
- `--max-messages N` — limit the full or date-bounded message scan to a cumulative,
  nonnegative number of processed messages (`0` means unlimited). Retry with a larger
  value or `0` to continue from the saved page checkpoint. This does not bound an
  incremental history sync or the history replay that finishes any completed full
  scan, including the initial scan.

For a bounded rehearsal, use a fresh isolated cache and isolated configuration with
an owner-only copy of the credential. A different `--db` path alone does not isolate
credentials: refreshing OAuth can rewrite the configured token file. Apply an overall
timeout in addition to a message limit, and verify the selected sync mode and persisted
counts rather than treating the option as a universal resource bound.

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

For a fictional cache with two threads labeled `ref/acme-corp` and one labeled
`ref/global-pay`, the output is:

```
Upserted 3 thread-account associations across 2 account(s):
  acme-corp: 2 threads
  global-pay: 1 threads
```

**Generate Gmail intelligence reports:**

```
fieldkit gmail enrich-pursuits
```

This reads your synced Gmail database and writes a `gmail-intel.md` report to each
selected configured account that has pursuit Markdown files
(`accounts/<account>/gmail-intel.md`). Accounts without pursuits are skipped. Each report
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

Check that the configured managed cache is verified and ready for local queries:

```
fieldkit doctor gmail
```

This resolves the configured `gmail_db` path. Pass `--db PATH` to inspect a
different cache. Exit `0` means the verified managed snapshot is query-ready and
has the required tables. Exit `1` means an active writer, a resource limit, or a
not-ready cache makes the check retryable; wait for sync to finish or resolve the
reported resource problem. Exit `3` means invalid or unverified local data needs
attention. This check does not establish mailbox
freshness or perform a complete SQLite integrity check. Run `fieldkit gmail sync`
when you need to refresh cached messages.

## Common errors

**OAuth authorization revoked or token unusable:**

Ordinary access-token expiry is handled automatically. If authorization has been
revoked or the saved token cannot be used, first restore your OAuth client settings.
If reauthorization is necessary, stop jobs using Google credentials and identify
the effective `gmail_token` path in your configuration. Move that file to a
private, owner-only backup location before retrying in an interactive terminal.
Do not guess its path or post its contents. The token is shared with other Google
commands, so they may also need authorization again. With the unusable token
safely set aside, fieldkit prints an authorization URL:

```
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

This starts a full sync from scratch. Subsequent runs use incremental history only
after the database has completed the full scan and its history reconciliation.

**Database unreadable or missing required tables:**

```
fieldkit doctor gmail
```

Check the effective `gmail_db` path and its permissions first. An unreadable cache
is not necessarily corrupt. Do not build missing tables or edit publication
identity metadata by hand.

Before rebuilding, stop every reader, writer, scheduled job, and other process
using the cache. Create an unused, owner-only backup directory on the same
filesystem. Rename the original database, any adjacent `-wal` and `-shm` files,
and its complete sibling publication directory into that directory as one
preserved group. For `gmail.db`, the publication directory is
`gmail.db.publication`. Keep the group private: it contains email data.
Same-filesystem renames preserve the original database inode; copies do not
guarantee a writable restoration of a managed publication. Never overwrite an
existing backup or try to rebind its identity.

Confirm both the original database path and publication-directory path are absent
(not dangling symlinks), with no sidecars left behind, before creating a new cache
at the configured path:

```
fieldkit gmail sync
```

Retain the group until sync completes and `fieldkit doctor gmail` verifies the
replacement. To roll back, stop all cache users again. Rename the complete
replacement group into a different unused private directory on the same
filesystem, then rename the original database, saved sidecars, and complete
publication directory back to their exact original paths. Do not merge groups,
restore only the database, or hand-edit identities. Verify with doctor before
restarting users.

## Import a supported legacy cache

Use `import-cache` only for a supported legacy Gmail schema, not a managed backup.
A managed database contains `_fieldkit_publication` and is rejected as an import
source. Restore a managed backup by the same-filesystem group rename described
above instead.

Stop users of the legacy source and retain it unchanged. Choose a fresh target
whose real parent directory already exists; its database, sidecars, and sibling
publication directory must not exist. First validate without creating the target,
then import and check the new managed cache:

```console
fieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db --dry-run
fieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db
fieldkit doctor gmail --db ./managed/gmail.db
```

Unknown schemas, ambiguous or changing inputs, and occupied destinations fail
closed. Preview success is not an import: the actual command validates again.
Neither command adopts or modifies the legacy source in place.
