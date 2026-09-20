# Slack Search Protocol

Reference for searching Slack as a signal source. Based on live exploration of `slackcli`
against your organization's internal workspace.

## Access

`slackcli` at `<user-home-path>/.linuxbrew/bin/slackcli`.
Auth tokens auto-loaded from `<fieldkit-workspace>/.env`.
No MCP server — CLI only.

Check auth: `slackcli auth list`

## What Works vs. What Doesn't

| Command | Status | Notes |
|---|---|---|
| `search messages` | ✅ Works | Main entry point — supports Slack operators |
| `search channels` | ✅ Works | Find channel IDs by name |
| `search people` | ✅ Works | Returns name, username, email |
| `conversations read <channel-id>` | ✅ Works | Read any channel/DM/group-DM you're in |
| `conversations unread` | ✅ Works | Lists all channels with unread messages |
| `conversations get <channel-id> <ts>` | ✅ Works | Fetch one message by timestamp |
| `canvas list` / `canvas read` | ✅ Works | List/read Slack canvas documents |
| `conversations list` | ❌ Blocked | `enterprise_is_restricted` — use `search channels` + `conversations read <channel-id>` instead |

## Search Commands

### Message search (primary)

```bash
slackcli search messages "<query>" --limit 20
```

**Supported Slack search operators — use these for precision:**

```bash
# By user
slackcli search messages "from:your-username <account-slug>"

# By channel
slackcli search messages "<account-slug>" --in team-acme-corp-core

# By date (ISO 8601)
slackcli search messages "<account-slug> after:2026-05-01"
slackcli search messages "<account-slug> before:2026-06-01"

# Combined
slackcli search messages "contract <account-slug> after:2026-04-01 has:link"

# Has attachment/link
slackcli search messages "<account-slug> has:link"
slackcli search messages "<account-slug> has:attachment"
```

**Sort options:**
```bash
--sort score       # most relevant first (default: timestamp)
--sort-dir desc    # newest first (default)
```

**Pagination:**
```bash
--page 2           # results are paged; check "Page X of Y" in output
```

### Find an account's dedicated channels

```bash
# Always start here — account channels have far higher signal-to-noise
slackcli search channels "<account-slug>"   # returns 57 channels; note [joined] ones
slackcli search channels "<account-slug>"
```

Look for patterns: `team-<account>-*` (delivery), `proj-<account>-*` (project),
`critsit-*-<account>-*` (incidents). These are high-signal, low-noise.

### Find a person

```bash
slackcli search people "Chris Roadfeldt"
slackcli search people "colleague@example.com"
```

Returns: display name, username (@handle), user ID, email, title.

## Reading Channels Directly

Once you have a channel ID (from `search channels` or a permalink URL):

```bash
# Read recent messages
slackcli conversations read <channel-id> --limit 20

# Read a specific thread (convert permalink timestamp: p1780572253421029 → 1780572253.421029)
slackcli conversations read <channel-id> --thread-ts 1780572253.421029

# Fetch a specific message
slackcli conversations get <channel-id> <timestamp>

# Read with time range
slackcli conversations read <channel-id> --oldest 1780000000 --limit 50
```

**[no text] results in search:** These are reactions, file posts, or bot messages.
Use `conversations get <channel-id> <ts>` to fetch the full message and see attachments.
Or read the channel directly with `conversations read --thread-ts` to get thread context.

## Check Unread Messages (daily sweep alternative)

```bash
slackcli conversations unread
```

Returns all channels with unread messages — including private account channels and
group DMs. Scan for account-relevant channel names (<account-slug>-*, team-*).
This is lower-noise than keyword search for "what happened today."

## Canvases (team docs in Slack)

```bash
slackcli canvas list
slackcli canvas read <canvas-id>
```

Canvases are collaborative documents attached to channels — may contain prep docs,
key resource lists, or account runbooks. Check if relevant to the account.

## Cached Signals File

`/brief`'s update op (`ops/update.md`) sweeps Slack daily and writes to:
`<fieldkit_home>/watchers/slack-signals.md`

**Read the cache first** — it covers the last 48h across all active accounts.

```bash
cat <data-repo>/watchers/slack-signals.md | grep -i "<account-slug>" -A5
```

If the cache is stale (> 12h) or you need a specific person not in the sweep,
run live searches directly.

## What Slack Actually Tells You

Slack is **internal only** — customers are not in this workspace.
Slack signals tell you what your *colleagues* are seeing and saying about an account,
not what the customer is doing.

| Signal type | What Slack reveals |
|---|---|
| Deal blockers | SAs, CSAs, or delivery team raising concerns in project channels |
| Critsit / escalations | `critsit-*-<account>-*` channels — active production incidents |
| Competitive intel | Field reps reporting competitor activity at the account |
| Champion posture | What RH colleagues say about a contact's behavior or influence |
| Delivery health | Delivery team threads in `team-<account>-*` channels |
| Executive signals | Account team DMs, meeting prep channels, VG prep channels |
| Internal blockers | Legal, deal desk, or procurement concerns in account channels |

## Recommended Search Strategy

**Fast path (most use cases):**
1. Check `slack-signals.md` cache for account name
2. If stale or missing: `slackcli search messages "<account>" after:<7-days-ago> --limit 20`

**Deep path (before a major meeting, QBR, or deal review):**
1. `slackcli search channels "<account>"` — find dedicated account/project channels
2. `slackcli conversations read <team-channel-id> --limit 20` — read `team-<account>-*`
3. `slackcli conversations unread` — check for unread in account channels
4. `slackcli search messages "from:<team-colleague> <account>"` — specific colleague's posts
5. `slackcli search messages "<competitor> <account>" after:<30-days-ago>` — field competitive intel

## Output Notes

- `--json` mixes status text into stdout — do NOT pipe directly to `python3 -c json.load(sys.stdin)`.
  Strip the first two lines or parse with: `slackcli ... --json 2>&1 | grep -v '^\-\|^✔' | python3 ...`
- "No results" is a valid signal — note it explicitly in output
- Do NOT surface confidential internal messages in customer-facing content

## Constraints

- `slackcli messages send/react/draft` — blocked by `outbound_gate`; never send autonomously
- `conversations list` — blocked by enterprise policy; use `search channels` instead
- DM listing blocked — but DMs and group DMs (mpdm-*) are readable by channel ID via `conversations read`
- Rate limits: if hit, note inline and continue
- Tokens expire with browser session — if auth fails, run `slackcli auth extract-tokens` for refresh guide
