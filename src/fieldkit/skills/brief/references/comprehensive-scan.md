# Comprehensive Scan Details

## Scan Activity Sources

Gather from sources available in this project:

- **Gmail cache:** Run `fieldkit gmail sync`, then `fieldkit gmail query account <account> --since $(date -d "7 days ago" +%Y-%m-%d)` per account
- **Calendar:** `gws calendar events list` for the next 5 business days
- **Backstory:** `get_recent_account_activity` per account for last 30 days
- **Pursuit files:** Read frontmatter for stage changes, close date shifts

## Flag Missed Todos

Compare activity against TASKS.md. Surface untracked action items:

```
## Possible Missing Tasks

1. From email thread with <contact-name> (May 2):
   "I'll send the updated SOW redlines by Friday"
   → [Acme Corp / Platform Modernization] Add to TASKS.md?

2. From meeting "Midwest Insurance QBR Prep" (May 1):
   QBR scheduled for May 15 but no prep task in TASKS.md
   → Add meeting-prep task?

3. From Backstory risk signal (Globalpay):
   "No executive engagement in 21 days"
   → Add outreach task for Globalpay exec sponsor?
```

Let user pick which to add.

## Engagement Health

Run decay analysis per account:

```
fieldkit gmail decay
```

Cross-reference with Backstory signals:
- Accounts with declining email frequency
- Accounts active in Backstory but quiet in email (or vice versa)
- Pursuits with close dates approaching but low recent engagement

## Surface New Contacts

Run people extraction per account:

```
fieldkit gmail query blindspots <account>
```

Surface frequent contacts not in stakeholder maps:

- High-frequency contacts not in `account.md` → suggest adding
- New names in pursuit-related threads → potential buying committee members
- People who dropped off (were active, now silent) → flag for relationship check

## Suggested Cleanup

- Pursuits with no activity in 30+ days and no close date → suggest archiving or updating
- Waiting On items past follow-up date by 7+ days → escalate
- Tasks referencing people not in any stakeholder map → fill the gap

## Notes

- Never auto-add without user confirmation
- If a source isn't available (auth expired, script error), skip and note the gap
- Fuzzy matching on task titles handles minor wording differences
- Safe to run frequently — only surfaces new information
