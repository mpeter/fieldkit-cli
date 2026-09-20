# gmail-cache

Syncs Gmail messages into a local SQLite database for offline analysis.

## What gets synced

All messages except those whose **only** labels are:
`SPAM`, `TRASH`, `CATEGORY_PROMOTIONS`, `CATEGORY_SOCIAL`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS`

Messages with mixed labels (e.g. SPAM + INBOX) are included.

Stored data: sender, recipient, CC, subject, date, labels, plain/HTML body, attachment metadata (no attachment content downloaded).

## Requirements

- Python 3.11+
- `uv sync --all-extras --dev` (dependencies managed via pyproject.toml)
- A Google Cloud project with the Gmail API enabled
- OAuth 2.0 credentials (Desktop app type)

## First-run setup

1. Create a Google Cloud project at <https://console.cloud.google.com>.
2. Enable the **Gmail API**.
3. Create **OAuth 2.0 credentials** → Desktop app → download JSON.
4. Run `fieldkit init` to configure credentials and data paths.

OAuth token is cached to `data/token.json` and reused on subsequent runs.

## Usage

```bash
# Full sync (all mail)
fieldkit gmail sync

# Limit to N messages (useful for testing)
fieldkit gmail sync --max-messages 500

# Custom DB path
fieldkit gmail sync --db /path/to/output.db
```

## Checkpoint / resume

The sync writes a checkpoint to the `sync_state` table after each batch. If interrupted (Ctrl+C, crash), restart with the same command — it resumes from the last checkpoint.

```bash
sqlite3 data/gmail.db "SELECT key, value FROM sync_state;"
```

Keys written: `last_page_token`, `messages_synced`, `sync_started_at`, `initial_sync_complete`, `last_history_id`.

## Data location

The database and token are written to `data/` (gitignored — contains PII and auth credentials).

- `data/gmail.db` — SQLite database
- `data/token.json` — cached OAuth token

## Querying

```bash
# Threads involving a person (name or email fragment)
fieldkit gmail query person "Rahul"

# Pre-meeting context: threads with body excerpts
fieldkit gmail query context "Lacey" --excerpt 300

# Account threads (tagged via ref/* label)
fieldkit gmail query account global-pay --since 2025-01-01

# Deal archaeology: account + keyword (searches subjects AND bodies)
fieldkit gmail query dig global-pay "SOW" --limit 20

# Thread subject search
fieldkit gmail query threads "OpenShift"

# Champion signal: initiation rate, last contact, silence duration
fieldkit gmail query champion "Chandra"

# Blind spots: external contacts active in account email but not in account.md
fieldkit gmail query blindspots global-pay --min-messages 5 --limit 20
```

All subcommands accept `--since YYYY-MM-DD`, `--before YYYY-MM-DD`, `--limit N`.

## Relationship Decay Report

```bash
# Contacts silent for more than N days (default: 90)
fieldkit gmail decay global-pay --days 60

# Show all contacts including active ones
fieldkit gmail decay shield-ins --all
```

Output columns: email, display name, days silent, days since they last wrote,
message count, thread count, signal (COLD / cooling / active).

## Schema

Seven tables: `threads`, `messages`, `attachments`, `sync_state`, `labels`, `people`, `thread_accounts`. WAL mode enabled.

```
threads         — thread_id, subject, snippet, updated_at
messages        — message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_epoch, labels, body_plain, …
attachments     — attachment_id, message_id, filename, mime_type, size_bytes
sync_state      — key/value checkpoint store
labels          — label_id, label_name (includes ref/* account labels)
people          — email, display_name, first_seen, last_seen, message_count
thread_accounts — thread_id, account (global-pay / acme-bank / shield-ins)
```

See `schema.sql` for the full DDL.
