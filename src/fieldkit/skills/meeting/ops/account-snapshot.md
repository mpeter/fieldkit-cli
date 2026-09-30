# Account Snapshot

Prepare a short weekly account brief from the configured fieldkit workspace and
local Gmail cache. Use it to identify current delivery and pursuit risks before
planning a meeting. The snapshot is a reviewed draft, not a live CRM writeback.

## Inputs and scope

The skill accepts one configured account slug. `--all` means repeat the workflow
for each configured account, and `--since YYYY-MM-DD` chooses the start of the
email window; both are skill-request options, not flags on a single `fieldkit`
command. `--no-backstory` means omit optional Backstory research. If the user
does not supply a date, use the last 14 days in the user's local timezone.

Read the configured workspace, not the repository checkout. If an account is
unknown or its data root is unavailable, report that condition instead of
creating a blank snapshot. Do not modify account or pursuit source files. Show
the draft and ask for confirmation before saving it under the account's
`meetings/` directory; do not send it externally.

## Gather local signals

1. Read project health from the configured workspace.

   ```console
   fieldkit pursuit projects --account <account> --json
   ```

   Use its reported `ZOMBIE`, `EXPIRING`, `SOON`, `ACTIVE`, and `UNKNOWN`
   classes. `EXPIRING` is within 30 days; `SOON` is within 90 days. A missing
   end date is `UNKNOWN`, not healthy. Do not infer capacity utilization from
   this command.
2. Read pursuit health from the configured workspace.

   ```console
   fieldkit pursuit health --account <account> --json
   ```

   Include each returned pursuit's stage, days in stage when available, close
   date, risk tier, and risk reasons. Current native qualification is
   `unavailable` in this local report; use an authorized live Salesforce read
   if it is needed. Do not substitute a historical local score.
3. Read matching thread summaries from the existing local Gmail cache.

   ```console
   fieldkit gmail query account <account> --since <YYYY-MM-DD> --limit 10 --json
   ```

   This report returns up to 10 recent matching cached threads. Its count is
   the returned rows, not the total matching threads; a short or empty result
   cannot establish complete source coverage or absence of activity. It also
   does not establish sender direction, the last inbound message, or
   unanswered-message counts.
   If the cache or account index is unavailable, mark the email signal
   unavailable. Read the bounded cache-derived contact recency signal next.

   ```console
   fieldkit gmail decay --account <account> --limit 10 --json
   ```

   The decay report calculates recency only from messages in threads tagged to
   the selected account. A newer message involving the same contact in another
   account does not make this account's relationship current. Do not call a
   contact COLD or WARM without this report's classification.

   The command defaults to `--max-age-days 365`, excluding contacts whose latest
   account-tagged message is older than a year. Even a complete empty result
   does not establish that there are no older stale contacts. If that older
   period matters, review a wider `--max-age-days` window and its scan bounds.

   Read its bounds separately. `truncated: true` means the contacts found by this scan
   exceeded the requested result limit; both flags can be true.
   `scan_truncated: true` means
   the cache work budget was exhausted: the command emits the bounded report,
   exits `1`, and the listed contacts are incomplete. Record `scanned_rows` and
   do not treat an empty incomplete report as evidence that no stale contact
   exists.
4. Read relevant watcher alerts from `watchers/` under the configured workspace,
   recording their date and account match. An absent watcher file means no
   watcher evidence, not proof that no risk exists.

These local commands report only the configured workspace and published cache.
Record each source's observation date, scope, and completeness. Credentialed
and optional sources require their own configured and authorized identity;
local report success does not establish that a live integration is available.

## Optional account intelligence

If the operator has authorized and configured a Backstory client,
and did not request `--no-backstory`, inspect account status and recent
activity. Record the source and observation date. Treat scores, risks, and
suggested next steps as unverified account-intelligence signals. If the route
is missing, unavailable, or omitted, write `Backstory Signal: unavailable`;
continue from the available local evidence. Never claim a live Backstory read from a
local cache or infer a score from an absent result.

## Draft and review

Use these sections, omitting empty rows rather than inventing account facts:

- **Active Delivery:** project name, end date, reported health class, and
  source date. Call out `ZOMBIE`, `EXPIRING`, and `UNKNOWN` explicitly.
- **Active Pursuits:** pursuit name, stage, days in stage if known, close date,
  reported risk and reasons; native qualification remains unavailable unless a
  separate live read was made.
- **Email Signal:** date window, matching threads and contacts from the local
  cache, and recency flags from the decay report. Mark unavailable inputs.
- **Backstory Signal:** dated, attributed findings or `unavailable`.
- **This Week's Priorities:** actions tied to the evidence above, with an
  owner or a question when ownership is unknown.

For `--all`, generate one reviewed draft per configured account and summarize
which accounts have evidence gaps. A draft already present at the intended path
requires confirmation before replacement. Do not imply that creating a brief
refreshes Gmail, Salesforce, Backstory, or watcher state.

Bound reads to the selected accounts, date window, and approved sources. Validate
each save destination within the configured workspace, rejecting traversal and
symlink escapes. Save approved private drafts atomically as UTF-8, reread them,
and report only verified writes. Do not upload or share private source material
without separate authorization.
