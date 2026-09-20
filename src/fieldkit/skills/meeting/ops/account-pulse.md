# Account Pulse

A rapid signal check across every active account in `config/accounts.yaml`. Uses Backstory
to surface what's moving, what's at risk, and where to put energy today.

**Distinct from /brief** — /brief is the single start-of-day synthesis (whispers, Today,
Slack threads) built from `brief.json`; this skill is the cross-account *portfolio* read
built from live Backstory + blindspot signals. Run /brief daily; run this before planning
sessions and pipeline calls.

## Gotchas

- **Trigger overlap** — "pulse" alone means /brief (start-of-day); "account pulse" / "how are my accounts" means this skill; a deep single-account brief is account-snapshot
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Inputs

None required — runs across all active accounts (non-`internal: true` entries in
`config/accounts.yaml`) by default.

Optional: "pulse on [account]" to focus on one.

Groups needed: **fieldkit-sales** (Backstory). Account/project files are read directly from disk (native file reads).

## Execution

### Step 0: Load account context (dossier-first)

Before pulling live signals, check for a pre-synthesized account dossier:

```
read accounts/<account>/dossier/dossier.md
```

If the dossier exists, use it to seed account context (contracts, key contacts,
recent email intelligence, strategic priorities). This avoids re-deriving context
already captured. Note the dossier's `Generated:` date — if older than 14 days,
treat it as background context only and rely on live signals for current state.

Then read directly from disk:
- `accounts/<account>/account.md` — stakeholder map, strategic priorities, scope limits
- `config/accounts.yaml` — account config (domains, thresholds, keywords)

### Step 1: Pull Backstory signals for each account

For each active account (or the one specified) via **fieldkit-sales** group:

```
1. backstory__backstory__find_account(<account name>)
   → get peopleai_account_id

2. backstory__backstory__get_account_status(peopleai_account_id)
   → health, risks, next steps, trending topics

3. backstory__backstory__get_recent_account_activity(peopleai_account_id)
   → what's been discussed in last 30 days

4. backstory__backstory__account_company_news(peopleai_account_id)   [public companies only]
   → trigger events, industry pressures
```

Also search Slack for account mentions:
```
slackcli search messages "<account name>" --limit 20
```
If Slack results contain unknown user IDs, resolve them:
```
slackcli search people "<name or email>"
```

Account context was loaded in Step 0 — reference that for open pursuits and strategic context.

Enumerate project files directly:
```
ls accounts/<account>/projects/*.md
```
Read each file for frontmatter fields.
Skip any project where `sf_contract_end` is empty or absent. Flag any project where
`sf_contract_end` is within 60 days (expiring soon) or past (expired). Note the project
type — Consulting Unit projects with approaching end dates may need redemption
acceleration or renewal conversations.

### Step 1b: Check contact blindspots

Read `config/accounts.yaml` and get `blindspots_min_messages` and `blindspot_days` for each account.

For each account, run the blindspots query:
```bash
fieldkit gmail query blindspots <account>
```

This surfaces contacts active in account email but absent from the account.md stakeholder map.
Use `--min-messages` to match the `blindspots_min_messages` threshold from accounts.yaml (default: 3).

Flag contacts that appear in Gmail threads for this account but are not listed in
`accounts/<account>/account.md` — potential relationship blind spots.

Add to the output under a **Contact Blindspots** section:
```markdown
## Contact Blindspots

### [Account]
- **[Name]** ([email]) — [N] messages, last contact [N] days ago
  Not in stakeholder map. Consider adding or confirming role.
```

If no blindspots found, omit this section.

### Step 1c: Backstory gap — first-party contacts invisible to CRM

For each account (or the one specified), run:
```bash
fieldkit gmail backstory-gap --account <account>
```

If `--account` is not supported for a specific account name, run without the flag and filter output by account section headers.

The script compares contacts visible in Gmail/Calendar/Slack against Backstory CRM. Include its output under a **Backstory Gap** section in the pulse:
```markdown
## Backstory Gap

### [Account]
[N] contacts visible in first-party signals but invisible to Backstory:
- **[Name]** ([email]) — [N] messages, [N] meetings — not in CRM
  ...
```

If the gap report is empty for an account, omit that account's subsection.

### Step 2: Classify each account

Assign each account to one bucket based on signals:

- **Needs action** — overdue commitments, Backstory-flagged risks, or stalled activity
- **Active** — healthy engagement, conversations in flight
- **Trigger** — news or change worth engaging on
- **Quiet** — low recent activity, may need proactive outreach

### Step 3: Output

```markdown
# Account Pulse — [Date]

---

## Needs Action Now

### [Account]
- **Risk:** [What Backstory flagged]
- **Move:** [Specific action — who to contact and why]

---

## Active Conversations

### [Account]
- [Key topic in flight]
- [What's being discussed / what's next]

---

## Trigger Events

### [Account]
- **[News item]** — [Why it's relevant: outreach hook, risk, or expansion angle]

---

## Delivery Alerts

### [Account]
- **[Project Name]** — contract ends [date] ([N] days) — [action needed: renew / redeem CUs / extend]

---

## Quiet — Consider Outreach

### [Account]
- Last touch: [date from Backstory]
- Suggestion: [one-line outreach idea tied to something relevant]

---

## Today's Priority

**Lead with:** [Account + one-sentence reason]
```

## Related Skills

- The main `meeting` skill (this skill's root) — Deep prep for any specific meeting surfaced in the pulse
- **grill** — Score any at-risk deal flagged in the pulse
- **workstream-discover** — Dig deeper on any expansion signal that warrants follow-up
