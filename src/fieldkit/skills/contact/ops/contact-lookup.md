# contact-lookup

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Description

Given an email address or display name, produce a unified contact profile by querying
the Gmail cache database and scanning pursuit/account markdown files.

## Trigger Phrases

Use this skill when the user says any of:

- `/contact-lookup <email or name>`
- "look up [name]"
- "who is [email]"
- "find contact [name]"
- "what do we know about [name or email]"
- "pull up the profile for [contact]"
- "check [email] in the contact database"
- "is [name] in any of our pursuits?"

## Execution Steps

1. **Run the CLI** with `--affiliations --json` to get a merged JSON payload:
   
   ```bash
   fieldkit contact find "<query>" --affiliations --json
   ```
   
   Replace `<query>` with the email address or display name provided by the user.

2. **Search Slack for internal intel about this contact** — Slack is internal
   only; what you're looking for is what *colleagues* have said about this person:

   ```bash
   # Check daily cache first (written by /brief's update op)
   grep -i "<display_name>" <data-repo>/watchers/slack-signals.md

   # Search what colleagues have said about this person by name
   slackcli search messages "<contact display name>" after:<90-days-ago> --limit 15

   # Find account-specific channels for delivery/deal context
   slackcli search channels "<account name>"
   slackcli conversations read <team-channel-id> --limit 15
   ```

   Look for: colleagues mentioning this person's name in project channels (champion
   behavior, resistance, title changes, departures), critsit channels involving them,
   or internal notes about their influence. "No results" is also signal — note it.

   See `../references/slack-search-protocol.md`.

3. **Parse the JSON response** — the `type` field determines the outcome:
   
   - `resolved` — full profile available; display the contact card
   - `ambiguous` — multiple matches; present the candidates table and ask the user to pick one
   - `not_found` — no contact in database; report the miss and check affiliations separately

3. **Format the contact card** for `resolved` responses using these sections in this order:
   
   - **Identity**: display_name, email, domain, account, is_internal
   - **Pursuit Affiliations**: list from `affiliations[]` showing pursuit_file, title, meddpicc_role, and notes. Lead with this — it answers "who is this person in the context of our business?"
   - **Recent Threads**: list from `recent_threads[]` with date and subject. Group or summarize if the threads cluster around a project or topic. This is the most useful context — synthesize what the conversation has actually been about.
   - **Recent Meetings**: list from `recent_meetings[]` with date and event summary
   - **Communication**: message_count, thread_count, initiated_count, meeting_count, first_seen, last_seen
   - **Internal Slack Intel**: what colleagues have said about this person — mentions in project channels, delivery threads, or deal discussions; "no mentions" is also reported
   - **Signals**: champion_signal (INITIATOR/MIXED/REACTIVE), decay_signal (ACTIVE/DECAY/GONE) — include a 1–2 sentence interpretation of what these signals mean for the relationship (e.g., REACTIVE + DECAY means we're doing all the outreach and the relationship is cooling)

4. **Handle `ambiguous`**: present the candidates table (email, display_name, account, message_count),
   ask the user to pick one, then re-run with the chosen email.

5. **Handle `not_found`**: report "No contact found for: <query>". If affiliations were returned,
   still display them — the contact may appear in pursuit files without being in the email database.

## Output Format

Present results as a formatted markdown card in chat. Do not write results to disk unless
the user explicitly requests it (e.g., "add this to the meeting notes").

The card should read like a briefing, not a data dump. After the raw stats, add a
**"So what"** line: one sentence synthesizing the relationship posture for this contact
based on the signals, recent threads, and affiliations. Example: "Brooke is a delivery
recipient on the RHOAI project — she receives status reports, engages occasionally, and
hasn't initiated contact in months. She's not a champion, but she's visible to us."

## Notes

- The CLI resolves the gmail db path from `~/.config/fieldkit/config.yaml` (`gmail_db` key) automatically. Use `--db <path>` only to override.
- `--affiliations` scans all `accounts/*/pursuits/*.md` and `accounts/*/account.md` files.
  It does not require a database connection.
- `recent_threads` returns up to 8 distinct conversation topics, deduped by subject stem
  (Re:/Fwd: stripped), OOO messages excluded. This is the most valuable signal for
  understanding what the relationship has actually been about.
- **Default lookback window: 12 months.** When synthesizing recent threads, examine dates
  and include context across the full 12-month window — not just the most recent 90 days.
  A contact's role and posture often only become clear over a longer arc (e.g., they may
  have driven procurement activity 6 months ago that is invisible in a 90-day view).
- Exit code 0 covers resolved, ambiguous, and not_found outcomes.
  Exit code 1 means an unhandled exception — report the stderr message to the user.
- For meeting prep, combine this skill output with the attendee's calendar entry for
  maximum context before a call.
