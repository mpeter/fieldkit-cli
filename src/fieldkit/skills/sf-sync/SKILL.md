---
name: sf-sync
description: >
  Salesforce data is stale and pursuit or account files need refreshing from SF before
  a call, review, or native ClosePlan qualification read. Pulls SF opportunity and account data into local
  frontmatter without requiring a browser.
  Trigger with "sync from Salesforce", "refresh SF data", "pull from SF",
  "SF is out of date", "update from Salesforce", "SF sync", "listview refresh",
  "opportunity data is stale", "sync SF for [account]", "pull SF opportunity [id]".
metadata:
  opencode/slash: "true"
  argument-hint: "[listview [account] | opportunity <opp_id> [file] | account <name>]"
  category: product
---

# sf-sync Skill

Sync Salesforce data into local pursuit and account files using high-level CLI commands.

Trigger with: `/sf-sync`, "sync salesforce", "refresh SF data", "pull SF data for [account]",
"update pursuit frontmatter from SF", "run sf-sync", "sync opportunity [id]".

---

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not advance deal stages without running the gate check first**
- **Always surface generated output for review before any external send**

## Modes

**listview** — bulk refresh all pursuits for one or all accounts via Salesforce list views
**opportunity** — sync a single pursuit's core Salesforce fields by Opportunity ID
**account** — sync all pursuits + account dashboard record for one account

If the user doesn't specify, ask: listview, opportunity, or account?
If they name an account, default to `listview <account>` or `account <name>` depending on context.
If they give a 15- or 18-char alphanumeric ID, default to `opportunity` mode.

---

## Step 0 — Pre-flight: verify SF session

Run `fieldkit sf session-check`.
- Exit 0: session active, proceed.
- Exit 2: session expired. Emit this message and STOP:
  "SF session expired. Get your `sid` cookie from your Salesforce org domain (DevTools → Application → Cookies), then run `fieldkit auth sf`. Re-run this skill once complete."
Do not proceed past this step if exit code is 2.

---

## Step 1 — Session Setup

Run once before any sync. The preferred method is fast and requires no browser:

```bash
fieldkit auth sf
```

Get `<sid>` from browser cookies at your Salesforce org domain (DevTools → Application →
Cookies → `sid` value). This injects the session cookie for both API and browser use.

If the browser flow is needed instead:

```bash
fieldkit auth sf
```

Verify the session is active:

```bash
fieldkit doctor sf
```

If verify fails, re-run `fieldkit auth sf` and retry. Do not proceed until
session is confirmed.

---

## Step 2 — Sync by Mode

### listview mode

Bulk refresh via Salesforce list views. Fully headless — no browser tools required.

```bash
fieldkit sf listview <account_name>   # one account
fieldkit sf listview                   # all accounts
```

Output: status, updated count, untracked count, errors.

**Untracked opportunities** — appear in list view but have no local pursuit file.
HCS Drawdown child opps are expected to be untracked. New pursuits need files created.

### opportunity mode

Sync a single pursuit by Salesforce opportunity ID. Fetches core fields only.

```bash
fieldkit sf opportunity <opp_id>              # auto-locate pursuit file
fieldkit sf opportunity <opp_id> <file>       # explicit pursuit file path
```

Fields updated: `sf_stage`, `sf_close_date`, `sf_arr`, `sf_owner`, `sf_next_steps`,
`sf_last_pulled`, `sf_acv`, `sf_consulting_acv`, `sf_training_acv`.

Native ClosePlan questions, answers, scores, and rollups are not written to pursuit
frontmatter. For current qualification, `grill` runs the separate read-only
`fieldkit sf meddpicc <opp_id> --json` contract.

### account mode

Sync all pursuits for one account plus the account dashboard record.

```bash
fieldkit sf account <name>
```

Where `<name>` is the account keyword from accounts.yaml (e.g. `acme-corp`, `<account-slug>`,
`midwest-ins`). Processes all pursuits with `sf_opportunity_id` in frontmatter, then updates
the account record with pipeline summary and services data.

Report: pursuits synced, skipped (no `sf_opportunity_id`), failed; account record updated.

---

## Error Handling

- **Auth failure:** Run `fieldkit auth sf`, then retry.
- **Untracked opportunity:** Opp exists in SF but no local pursuit file. Review after each listview sync. Create a pursuit file if the opp is actively being pursued.
- **Partial failure:** Individual pursuit errors are logged; remaining pursuits in the account continue. Never abort an entire account sync due to a single pursuit failure.
- **No sf_opportunity_id in pursuit:** Logged as SKIP — expected for pre-pipeline opps.

---

## Output Format

**Salesforce Sync Complete — [mode] — [timestamp]**

Per pursuit:

- File path
- Fields updated: `sf_stage`, `sf_close_date`, `sf_arr`, `sf_owner`, `sf_next_steps`, `sf_last_pulled`
- Status: ✅ synced | ⚠️ skipped (no sf_opportunity_id) | ❌ error

Per account record (account mode):

- File: `accounts/<name>/account.md`
- Status: ✅ | ❌

---

## Related Skills

- `grill` — uses the Opportunity link to read exact native ClosePlan questions; pursuit review also relies on `sf_stage` and `sf_close_date` being current
- the `pipeline` skill's `src/fieldkit/skills/pipeline/ops/engagement-health.md` — uses sf_contract_end from project files
- the `pipeline` skill's `src/fieldkit/skills/pipeline/ops/forecast.md` — depends on sf_arr and sf_close_date
