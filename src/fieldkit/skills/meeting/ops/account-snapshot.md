# Account Snapshot

1-page weekly status brief per account. Takes 60–90 seconds.
Answers: what are my projects doing, where are my deals, what's the email signal, what are the risks?

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Flags

- `[account]` — account slug (e.g. `/account-snapshot <account-slug>`)
- `--all` — run for all configured accounts (one snapshot file per account)
- `--since YYYY-MM-DD` — custom email lookback window (default: 14 days)
- `--no-backstory` — skip Backstory API calls (offline/fast mode)

Groups needed: **fieldkit-sales** (Backstory — skipped with --no-backstory).
Project + pursuit files and watcher alerts are read directly from disk (native file reads).

---

## Data Sources (in execution order)

### 1. Project health

```bash
fieldkit pursuit projects --account <account>
```

Read output and categorise each project:
- ✓ ACTIVE — end date > 30 days out, normal CU burn
- ⚠ SOON — end date within 30 days (renewal conversation needed)
- ☠ ZOMBIE — end date in the past (needs formal closeout)

### 2. Pursuit health

```bash
fieldkit pursuit health --account <account>
```

For each active pursuit, capture: deal name, stage, days in stage, close date,
risk tier, and Native Qualification. The local health command does not fetch
ClosePlan, so qualification is `unavailable`; do not substitute historical scores.

### 3. Email signal (14-day window)

```bash
fieldkit gmail query account <account> --since <14-days-ago>
```

Summarise:
- Last inbound from customer domain: who, when, subject
- Total threads in window (customer-initiated vs our-initiated)
- Any threads with no reply from us > 3 days old

Also run:
```bash
fieldkit gmail decay <account-domain>
```

Surface any contacts in COLD or cooling range who are named in active pursuits.

### 4. Backstory signal (skip if --no-backstory)

Via **fieldkit-sales** group:
```
backstory__backstory__find_account(<account name>)
backstory__backstory__get_account_status(peopleai_account_id)
backstory__backstory__get_recent_account_activity(peopleai_account_id)
```

Extract: engagement score, recent topics, risks flagged by Backstory.

If `--no-backstory` is set, skip all Backstory API calls and include this note in the output:
```
### 📊 Backstory Signal
_Backstory unavailable (--no-backstory mode). Run without this flag for live signals._
```

### 5. Watcher alerts

Read:
- `<fieldkit_home>/watchers/backstory-alerts.md` — filter to this account
- `<fieldkit_home>/watchers/pursuit-stall-alerts.md` — filter to this account

---

## Output Format

Saved to: `accounts/<account>/meetings/YYYY-WNN-weekly-snapshot.md`

```markdown
## [Account] — Week of [DATE]

### 🏗️ Active Delivery (Projects)
| Project | End Date | Health | Notes |
|---|---|---|---|
| [name] | YYYY-MM-DD | ✓ ACTIVE | On track |
| [name] | YYYY-MM-DD | ⚠ SOON | Expires in N days |
| [name] | YYYY-MM-DD | ☠ ZOMBIE | Past end date, needs closure |

### 🎯 Active Pursuits
| Deal | Stage | Days | Native Qualification | Close | Risk |
|---|---|---|---|---|---|
| [deal] | negotiate | 32d | unavailable — run `/grill` for live read | Nov-26 | ⚠ |

### 📧 Email Signal (Last 14 Days)
- Last inbound: [name] N days ago re: [subject]
- Threads: N total (N customer-initiated)
- Unanswered (>3d): N

### 📊 Backstory Signal
- Engagement score: [N]/100
- Recent topics: [brief]
- Risks: [list or 'none flagged']

### ⚡ This Week's Priorities
1. [action derived from stalls/expiring projects/risks]
2. ...
```

---

## When --all

Run for each account in `config/accounts.yaml`. Output one file per account.
Print a summary table of all accounts when done:

```
Account    Projects  Pursuits  Backstory  Alerts
<account-slug>       3 active  2 deals   72/100     2 stalls
<account-slug>  1 active  1 deal    45/100     1 zombie
```
