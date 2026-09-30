---
name: sf-sync
description: >
  Preview live Salesforce account or opportunity data, then refresh explicitly
  approved local fieldkit frontmatter with shipped Salesforce commands.
metadata:
  opencode/slash: "true"
  argument-hint: "[account slug | opportunity id | approved bulk account]"
  category: product
---

# Preview and refresh Salesforce-backed fields

Use this skill when the Salesforce integration is installed and the operator
needs live Salesforce data compared with, or copied into, the configured
fieldkit workspace. A request to “check” or “show” Salesforce data authorizes a
read, not a local write.

Never accept a session ID in chat, command arguments, environment variables,
logs, or saved evidence. Authentication is an operator-owned prerequisite.

## Verify the session

Run fieldkit sf session-check --json. Exit 0 with active: true proves the stored
credential passed the command's current Account metadata probe. Exit 2 means the
credential is missing, expired, rejected, or otherwise unusable for that probe;
do not continue with a Salesforce read or write.

For interactive reauthorization, the operator runs fieldkit auth sf and pastes
the sid only into the hidden terminal prompt. Non-interactive automation may use
fieldkit auth sf --sid-file PATH only with an owner-only regular file. After
authentication, rerun the session check. Do not record the cookie value.

## Preview without writing

Choose a single, confirmed scope and preview it before proposing a refresh:

- fieldkit sf opportunity OPPORTUNITY_ID --json fetches one opportunity and
  emits the mapped payload without updating frontmatter. A numeric Salesforce
  Opportunity Number is also accepted and resolved before the read.
- fieldkit sf opportunity OPPORTUNITY_ID PURSUIT_FILE --no-write prints a
  human-readable preview for an explicit local destination without writing it.
- fieldkit sf opportunity OPPORTUNITY_ID PURSUIT_FILE --dry-run is the
  equivalent preview spelling used by the CLI write contract.
- fieldkit sf account ACCOUNT --json fetches an account dashboard payload
  without updating account.md.
- fieldkit sf account ACCOUNT --no-write prints that account dashboard without
  writing it.
- fieldkit sf account ACCOUNT --dry-run is the equivalent preview spelling.
- fieldkit sf listview ACCOUNT --dry-run --json performs the account-scoped
  Salesforce reads and local ID matching without creating directories or
  writing cache, frontmatter, databases, or state. Its summary reports
  updated: 0, would_update, and dry_run: true.

Confirm that the returned Salesforce account and opportunity match the intended
local account and pursuit. Treat missing, conflicting, or ambiguous identity as
a blocker.

For listview, --json alone changes only the summary format and still writes.
The --dry-run flag is what suppresses those workspace writes.

## Show the proposed local change

Before a write, name the exact command, Salesforce record, local destination,
and mapped fields. For an opportunity refresh, the writer can update the tracked
Salesforce ID, name, stage, close date, owner, next steps, pull timestamp,
opportunity number, ARR, ACV, consulting ACV, training ACV, contract type, and
deal splits when present.

Those fields are Salesforce-derived local cache data. They are not native
ClosePlan questions, answers, scores, or rollups. Do not convert adjacent pain,
criteria, competitor, model, or historical local-score material into current
qualification evidence.

Obtain explicit approval for the displayed destination and fields when using a
single-opportunity or account preview. A listview dry run's JSON summary reports counts only;
normal stderr progress may name matched IDs and destination paths, but does not
show per-record mapped fields. Quiet mode may omit that progress. Treat its
approval as approval of the named account-wide scope, or use single-opportunity
previews when per-record review is required. Approval for one pursuit is not
approval for an account-wide or all-account refresh.

## Apply only the approved scope

- For one pursuit, use fieldkit sf opportunity OPPORTUNITY_ID PURSUIT_FILE.
  Supplying the destination avoids writing a similarly identified file by
  accident. An untracked write without a destination fails rather than creating
  a pursuit.
- For one account's bulk pursuit refresh, use fieldkit sf listview ACCOUNT only
  after explicit approval. It searches Salesforce, matches returned opportunity
  IDs to existing local pursuit files, writes cache and mapped frontmatter for
  matches, and reports open opportunities it could not match.
- Use fieldkit sf listview --all only after separate approval for every
  configured non-internal account. An omitted target also selects all accounts,
  so do not omit it.
- To update only one account dashboard record, use fieldkit sf account ACCOUNT.
  This writes the Salesforce cache and mapped account frontmatter in
  accounts/ACCOUNT/account.md; it does not refresh that account's pursuit files.

## Verify the result

Read every approved destination back and preserve unrelated frontmatter and body
content. For a single-opportunity or account refresh, compare mapped fields with
the corresponding preview payload. A listview preview has no per-record payload:
compare each changed pursuit with its corresponding fetched Salesforce record or
newly written local cache, and report any field whose source cannot be verified.
Report the pull timestamp, changed files, untracked opportunities, skips, and
errors.

For listview, exit 0 means the completed scope reported zero processing errors;
it does not mean every Salesforce opportunity had a local match. Exit 1 means
one or more search, scan, or write errors were summarized. Authentication
failures take precedence and exit 2. Check the structured summary and review
every untracked record separately.

Never create a pursuit file, choose a destination, edit an opportunity ID, or
retry a partial write silently. Present the unresolved item and ask for a new,
specific decision.
