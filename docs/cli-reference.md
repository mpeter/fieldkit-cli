---
caste: derived
derived_from:
- fieldkit --help (live CLI surface)
generated_by: scripts/generate_cli_docs.py
---

# fieldkit CLI Reference

> **Derived document — generated exhaust, not a system of record.** Verify facts against the sources listed in `derived_from`.

> Auto-generated 2026-09-20 from `fieldkit --help`. Do not edit manually.
> Re-generate: `uv run python scripts/generate_cli_docs.py`

## Quick reference

| Group | Key subcommands |
|---|---|
| `fieldkit auth` | `backstory`, `google`, `sf`, `shadowbot` |
| `fieldkit sf` | `account`, `components`, `frontmatter`, `listview`, `meddpicc`, `opportunity`, `quote`, `reconcile`, `schema`, `session-check`, `set-field`, `set-next-steps`, `update-closeplan` |
| `fieldkit gmail` | `account-tags`, `backstory-gap`, `decay`, `enrich-pursuits`, `query`, `sync` |
| `fieldkit pursuit` | `advance`, `archive`, `audit`, `create`, `forecast`, `health`, `projects`, `rename`, `repair-dates` |
| `fieldkit shadowbot` | `query` |
| `fieldkit watch` | `logs`, `run`, `status` |
| `fieldkit ingest` | `backfill`, `discover`, `promote`, `reprocess`, `route`, `run`, `status` |
| `fieldkit sync` |  |
| `fieldkit issue` | `board`, `close`, `create`, `edit`, `fix`, `link`, `list`, `note`, `plan`, `reopen`, `show`, `sync-milestone` |
| `fieldkit skill` | `eval`, `install`, `list`, `show`, `variables` |
| `fieldkit init` | `migrate` |
| `fieldkit brief` | `generate`, `open` |
| `fieldkit pipeline` | `open`, `quota` |
| `fieldkit version` |  |
| `fieldkit commands` |  |
| `fieldkit companion` | `allowed`, `feed`, `loop`, `prune`, `reconcile-invalid`, `run` |
| `fieldkit completion` |  |
| `fieldkit contact` | `enrich`, `find`, `list`, `report` |
| `fieldkit doctor` | `gmail`, `google`, `sf`, `shadowbot` |
| `fieldkit golive` |  |
| `fieldkit gtask` | `complete`, `create` |
| `fieldkit meeting` | `link`, `list`, `note`, `open` |
| `fieldkit web` | `serve`, `token` |

---

## `fieldkit auth`

```
Usage: fieldkit auth [OPTIONS] COMMAND [ARGS]...

  Authenticate fieldkit integrations.

Options:
  -h, --help  Show this message and exit.

Commands:
  backstory  Authenticate Backstory through MCPJungle's browser OAuth flow.
  google     Authenticate with Google OAuth for Gmail access.
  sf         Authenticate the Salesforce session via a sid cookie.
  shadowbot  Show ShadowBot auth status, or inject a refresh token.
```

### `fieldkit auth backstory`

```
Usage: fieldkit auth backstory [OPTIONS]

  Authenticate Backstory through MCPJungle's browser OAuth flow.

  MCPJungle stores and refreshes the upstream token. Rerun this command to
  replace the Backstory registration and authorize again.

Options:
  -h, --help  Show this message and exit.
```

### `fieldkit auth google`

```
Usage: fieldkit auth google [OPTIONS]

  Authenticate with Google OAuth for Gmail access.

  Runs the same credential resolution and consent flow that ``gmail sync``
  falls back to automatically — use this to authenticate up front instead of
  waiting for the first sync to prompt.

Options:
  -h, --help  Show this message and exit.
```

### `fieldkit auth sf`

```
Usage: fieldkit auth sf [OPTIONS]

  Authenticate the Salesforce session via a sid cookie.

  Without ``--sid-file``: walks through the DevTools steps interactively, then
  validates the session against the live Salesforce API.

  With ``--sid-file PATH``: reads an owner-only file containing the copied
  browser value and validates the session without prompting.

Options:
  --sid-file FILE  Read a sid from an owner-only regular file, skipping the
                   interactive prompt but validating the session.
  --json           Emit credential-safe authentication health as JSON.
  -h, --help       Show this message and exit.
```

### `fieldkit auth shadowbot`

```
Usage: fieldkit auth shadowbot [OPTIONS]

  Show ShadowBot auth status, or inject a refresh token.

  Without flags: checks whether a valid token is available and prints ``✓ auth
  active`` on success.

  With ``--refresh-token-file PATH``: reads an owner-only token file, saves
  the token to disk, and prints the path on stdout.

Options:
  --refresh-token-file FILE  Read a Keycloak refresh token from an owner-only
                             regular file.
  --json                     Emit credential-safe authentication health as
                             JSON.
  -h, --help                 Show this message and exit.
```

## `fieldkit sf`

```
Usage: fieldkit sf [OPTIONS] [COMMAND] [ARGS]...

  Salesforce pipeline — listview, opportunity, account, quote, meddpicc,
  frontmatter, reconcile, field writes.

Options:
  -h, --help  Show this message and exit.

Commands:
  account           Fetch and display the Salesforce account dashboard.
  components        Show CPQ component lines for an Opportunity id or...
  frontmatter       Write sf_* frontmatter fields to pursuit files.
  listview          Fetch and display Salesforce list view records.
  meddpicc          Read the ClosePlan/TSPC MEDDPICC scorecard for an...
  opportunity       Fetch and sync a single Salesforce opportunity to a...
  quote             Read a CPQ Quote and its quote lines.
  reconcile         Rewrite Key Fields table in a pursuit file from...
  schema            Render safe metadata and observed population for...
  session-check     Check whether the Salesforce session cookie is still...
  set-field         Write an approved field on a Salesforce record.
  set-next-steps    Write the Next Steps field on a Salesforce Opportunity.
  update-closeplan  Preview or confirm guarded native ClosePlan question...
```


> **Auth:** Salesforce uses the `sid` session cookie.
> - Authenticate: `fieldkit auth sf` (or use `--sid-file PATH` for an owner-only secret file)
> - Get `sid`: Chrome DevTools → Application → Cookies → your configured `my.salesforce.com` host
> - Check validity: `fieldkit sf session-check`
> - `set-next-steps` and `set-field` are **dry-run by default** — pass `--confirm` to write.

### `fieldkit sf account`

```
Usage: fieldkit sf account [OPTIONS] ACCOUNT_NAME

  Fetch and display the Salesforce account dashboard.

  ACCOUNT_NAME must match a key in accounts.yaml (e.g. acme, globalpay,
  midwestins).

  Fetches: account metadata (owner, segment, industry), open services pipeline
  with consulting/training splits per opportunity, and aggregate ACV totals.
  Writes results to accounts/<name>/account.md.

Options:
  --no-write  Print dashboard without updating account.md.
  --json      Machine-readable JSON output (suppresses human dashboard).
  -h, --help  Show this message and exit.
```

### `fieldkit sf components`

```
Usage: fieldkit sf components [OPTIONS] OPPORTUNITY

  Show CPQ component lines for an Opportunity id or number.

Options:
  --json      Emit deterministic component records as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit sf frontmatter`

```
Usage: fieldkit sf frontmatter [OPTIONS] [FILE] [JSON_STRING]

  Write sf_* frontmatter fields to pursuit files.

  Modes:
    FILE JSON_STRING          SF-mode: upsert Salesforce fields from JSON
    --quality-check --file F  Backstory advisory check
    --validate --file F       Schema validation

Options:
  --quality-check  Run Backstory quality checks.
  --validate       Validate frontmatter against the pursuit schema.
  --file FILE      Pursuit file path (required for --quality-check and
                   --validate).
  --json           Emit the result of the selected mode as JSON on stdout.
  --dry-run        Preview SF-mode changes without writing the file.
  -h, --help       Show this message and exit.
```

### `fieldkit sf listview`

```
Usage: fieldkit sf listview [OPTIONS] [TARGET]

  Fetch and display Salesforce list view records.

  TARGET is an account name (e.g. global-pay, acme-bank, shield-ins). Use
  --all to sync all accounts (default when no TARGET given). --territory
  overrides TARGET by resolving the sf_territory value to an account name.

  Exit codes:   0 — sync completed without errors   1 — partial failure (some
  accounts had errors)   2 — auth failure

Options:
  --all                  Sync all accounts (default when no TARGET is given).
  --territory TERRITORY  Salesforce territory name (sf_territory in
                         accounts.yaml). Resolves to the matching account.
  --json                 Emit JSON summary to stdout.
  --quiet                Suppress [sf-listview-sync] progress lines.
  --services-only        Qualify services opportunities from CPQ component
                         lines, including TAM.
  --limit INTEGER RANGE  Maximum candidates per services scan (default: 50).
                         [x>=1]
  -h, --help             Show this message and exit.
```

### `fieldkit sf meddpicc`

```
Usage: fieldkit sf meddpicc [OPTIONS] OPP_ID

  Read the ClosePlan/TSPC MEDDPICC scorecard for an opportunity.

  OPP_ID is the 15- or 18-character Salesforce Opportunity ID.

  Fetches every linked TSPC__Deal__c scorecard and question. When several
  deals are linked, use --deal-id to select one exact deal; none is selected
  from response order.

  Opportunities without a ClosePlan display a clear "no ClosePlan configured"
  message rather than an error.

Options:
  --deal-id TEXT  Select one exact linked ClosePlan deal ID; all linked deals
                  are still read.
  --json          Emit the scorecard as JSON.
  -h, --help      Show this message and exit.
```

### `fieldkit sf opportunity`

```
Usage: fieldkit sf opportunity [OPTIONS] OPP_ID [PURSUIT_FILE]

  Fetch and sync a single Salesforce opportunity to a pursuit file.

  OPP_ID is the 15- or 18-character Salesforce opportunity ID, or a numeric
  Salesforce Opportunity Number (e.g. 71721820) which will be resolved to the
  18-char ID via SOSL before fetching.

  PURSUIT_FILE is optional — if omitted, auto-resolved via match-pursuit.

  Fetches: stage, close date, owner, ACV/ARR, consulting/training/app services
  splits, next steps, and MEDDPICC-adjacent signals (identify pain, decision
  criteria, main competitor).

Options:
  --no-write  Print summary without updating frontmatter.
  --json      Machine-readable JSON output (suppresses human table).
  -h, --help  Show this message and exit.
```

### `fieldkit sf quote`

```
Usage: fieldkit sf quote [OPTIONS] QUOTE_ID

  Read a CPQ Quote and its quote lines.

  QUOTE_ID is the 15- or 18-character Salesforce SBQQ__Quote__c record ID.

  Reads the Quote header (number, status, net/list/customer amounts, average
  discount) via a record fetch, and the quote lines via the UI API related-
  list-records route, which remains compatible with deployments where CPQ
  objects cannot be queried through SOQL or SOSL.

Options:
  --json      Machine-readable JSON output (suppresses human summary).
  -h, --help  Show this message and exit.
```

### `fieldkit sf reconcile`

```
Usage: fieldkit sf reconcile [OPTIONS] PURSUIT_FILE

  Rewrite Key Fields table in a pursuit file from frontmatter values.

Options:
  --dry-run   Preview what would be changed without writing any files.
  --json      Emit the reconcile outcome as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit sf schema`

```
Usage: fieldkit sf schema [OPTIONS] SOBJECT

  Render safe metadata and observed population for explicit records only.

Options:
  --record-id ID  Explicit Salesforce record ID to inspect. Repeat for each
                  sample.  [required]
  --json          Emit the schema reference as JSON.
  -h, --help      Show this message and exit.
```

### `fieldkit sf session-check`

```
Usage: fieldkit sf session-check [OPTIONS]

  Check whether the Salesforce session cookie is still valid.

  Exits 0 when the session is active, 2 when expired or missing.

Options:
  --json      Emit result as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit sf set-field`

```
Usage: fieldkit sf set-field [OPTIONS] [RECORD_ID] [FIELD] [VALUE]

  Write an approved field on a Salesforce record.

  Only explicitly approved fields may be written. Use --list-fields to see the
  full allowlist. Any other field name is rejected before touching SF.

  Allowed fields — Opportunity:
    CloseDate                Close Date (YYYY-MM-DD)
    Next_Steps__c            Next Steps
    Bill_To_Account__c       Bill To Account #
    Country_of_Order__c      Country of Order
    EBS_Account_Name__c      Bill To EBS Account Name
    EBS_Bill_to_Account_Alias__c  EBS Bill to Account Alias
    Identify_Pain_Long__c    Identify Pain Long
    RouteToMarket__c         Route To Market

  Allowed fields — SBQQ__Quote__c:
    SBQQ__StartDate__c       Start Date
    SBQQ__EndDate__c         End Date
    SBQQ__ExpirationDate__c  Expires On
    Approval_Comments__c     Business Justification
    Gross_Margin__c          Gross Margin (number)

Options:
  --sobject TEXT  Salesforce object API name.  [default: Opportunity]
  --confirm       Actually write to Salesforce. Without this flag the command
                  only previews.
  --list-fields   List all allowed fields per object and exit.
  --json          Emit the write outcome as JSON.
  -h, --help      Show this message and exit.
```

### `fieldkit sf set-next-steps`

```
Usage: fieldkit sf set-next-steps [OPTIONS] OPP_ID [TEXT]...

  Write the Next Steps field on a Salesforce Opportunity.

  OPP_ID is the 15- or 18-character Salesforce Opportunity ID. TEXT is the new
  value for the Next Steps field.

  By default this command is a dry-run — it shows the current and proposed
  values without touching Salesforce. Pass --confirm to actually write.

Options:
  --confirm   Actually write to Salesforce. Without this flag the command only
              previews the change.
  --json      Emit the write outcome as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit sf update-closeplan`

```
Usage: fieldkit sf update-closeplan [OPTIONS] [OPPORTUNITY_ID]

  Preview or confirm guarded native ClosePlan question scores.

  Default mode creates a PII-minimized preview plan. Confirmation accepts only
  that plan identifier, rereads every binding, and stops on any conflict or
  uncertain response. It never writes answer fields or derived rollups.

Options:
  --deal-id TEXT     Exact ClosePlan deal Id for a preview.
  --score TEXT       Exact QUESTION_ID=NATIVE_SCORE assignment; repeat as
                     needed.
  --confirm PLAN_ID  Confirm one previously reviewed preview plan.
  --json             Emit structural JSON output.
  -h, --help         Show this message and exit.
```

## `fieldkit gmail`

```
Usage: fieldkit gmail [OPTIONS] [COMMAND] [ARGS]...

  Gmail cache pipeline — sync, query, and analyse Gmail data.

Options:
  -h, --help  Show this message and exit.

Commands:
  account-tags     List Gmail account tags
  backstory-gap    Find account context gaps
  decay            Report stale Gmail relationships
  enrich-pursuits  Enrich pursuits from the local Gmail cache
  query            Query the local Gmail cache
  sync             Synchronize Gmail through Google OAuth
```

### `fieldkit gmail account-tags`

```
Usage: fieldkit gmail account-tags [OPTIONS]

  Map Gmail threads to accounts via ref/* labels.

Options:
  --db TEXT           Path to gmail.db (defaults to data/gmail.db in
                      workspace).
  -a, --account TEXT  Filter tagging to a single account slug (processes only
                      ref/<slug> labels).
  --json              Machine-readable JSON output.
  -h, --help          Show this message and exit.
```

### `fieldkit gmail backstory-gap`

```
Usage: fieldkit gmail backstory-gap [OPTIONS]

  Backstory gap report — contacts visible in Gmail/Calendar/Slack but not in
  Backstory/CRM.

Options:
  --account TEXT          Restrict to a single account key (e.g. global-pay,
                          acme-bank, shield-ins). Default: all accounts.
  --min-messages INTEGER  Minimum message count threshold. Defaults to
                          blindspots_min_messages from config/accounts.yaml
                          per account.
  --db TEXT               Path to gmail.db  [default: (dynamic)]
  --json                  Machine-readable JSON output.
  -h, --help              Show this message and exit.
```

### `fieldkit gmail decay`

```
Usage: fieldkit gmail decay [OPTIONS] [ACCOUNT]

  Relationship decay report — last contact per external person per account.

Options:
  -a, --account TEXT      Account slug (preferred over positional argument).
  --days INTEGER          Days-silent threshold to flag as stale (default: 90)
  --all                   Show all contacts, not just stale ones
  --domain TEXT           Restrict to contacts at this domain (e.g.
                          globalpay.com)
  --min-messages INTEGER  Only show contacts with at least N messages
                          (default: 1)
  --limit INTEGER         Cap results to N contacts (default: unlimited)
  --max-age-days INTEGER  Exclude contacts silent for more than N days (0 = no
                          cutoff).  [default: 365]
  --db TEXT               Path to gmail.db  [default: (dynamic)]
  --json                  Emit the decay report as JSON.
  -h, --help              Show this message and exit.
```

### `fieldkit gmail enrich-pursuits`

```
Usage: fieldkit gmail enrich-pursuits [OPTIONS]

  Write gmail-intel.md files per configured account.

Options:
  --account TEXT  Scope to a single account slug.
  --json          Emit enrichment outcomes as JSON.
  -h, --help      Show this message and exit.
```

### `fieldkit gmail query`

```
Usage: fieldkit gmail query [OPTIONS] [COMMAND] [ARGS]...

  Query gmail.db by person, account, or thread subject.

Options:
  --account SLUG  Run the account summary query for this account slug.
  -h, --help      Show this message and exit.

Commands:
  account     Show thread summary and top contacts for an account.
  blindspots  People active in account email but not in account.md...
  champion    Champion signal: initiation rate, last contact, thread...
  context     Person threads with body excerpts — pre-meeting context.
  dig         Account + keyword — deal archaeology across a full history.
  person      Find threads involving a person by name or email.
  threads     Find threads by subject keyword.
```

#### `fieldkit gmail query account`

```
Usage: fieldkit gmail query account [OPTIONS] NAME

  Show thread summary and top contacts for an account.

Options:
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

#### `fieldkit gmail query blindspots`

```
Usage: fieldkit gmail query blindspots [OPTIONS] ACCOUNT

  People active in account email but not in account.md stakeholder lists.

Options:
  --min-messages INTEGER   Minimum message count to surface (default: 3)
                           [default: 3]
  --known EMAIL_FRAGMENTS  Comma-separated email fragments to exclude (known
                           contacts)
  --include-suspected      Include and mark addresses suspected of containing
                           masked data.
  --since YYYY-MM-DD       Include threads with messages on or after this date
                           (YYYY-MM-DD)
  --before YYYY-MM-DD      Include threads with messages before this date
                           (YYYY-MM-DD)
  --limit INTEGER RANGE    Max results (default: 50; must be ≥ 1)  [default:
                           50; x>=1]
  --db TEXT                Path to gmail.db  [default: (<data/gmail.db>)]
  --json                   Machine-readable JSON output.
  -h, --help               Show this message and exit.
```

#### `fieldkit gmail query champion`

```
Usage: fieldkit gmail query champion [OPTIONS] NAME

  Champion signal: initiation rate, last contact, thread history.

Options:
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

#### `fieldkit gmail query context`

```
Usage: fieldkit gmail query context [OPTIONS] NAME

  Person threads with body excerpts — pre-meeting context.

Options:
  --excerpt INTEGER      Max body chars per thread (default: 300)  [default:
                         300]
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

#### `fieldkit gmail query dig`

```
Usage: fieldkit gmail query dig [OPTIONS] ACCOUNT KEYWORD

  Account + keyword — deal archaeology across a full history.

Options:
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

#### `fieldkit gmail query person`

```
Usage: fieldkit gmail query person [OPTIONS] NAME

  Find threads involving a person by name or email.

Options:
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

#### `fieldkit gmail query threads`

```
Usage: fieldkit gmail query threads [OPTIONS] KEYWORD

  Find threads by subject keyword.

  Without --account this searches subjects across every account. With it,
  results are restricted to threads tagged to that account by `fieldkit gmail
  account-tags`, which populates thread_accounts — so a thread the tagger has
  not seen yet will not appear in a scoped search.

  Exit codes: 0 success; 3 unknown account slug.

Options:
  -a, --account SLUG     Limit results to threads tagged to a single account
                         slug. Default: all accounts.
  --since YYYY-MM-DD     Include threads with messages on or after this date
                         (YYYY-MM-DD)
  --before YYYY-MM-DD    Include threads with messages before this date (YYYY-
                         MM-DD)
  --limit INTEGER RANGE  Max results (default: 50; must be ≥ 1)  [default: 50;
                         x>=1]
  --db TEXT              Path to gmail.db  [default: (<data/gmail.db>)]
  --json                 Machine-readable JSON output.
  -h, --help             Show this message and exit.
```

### `fieldkit gmail sync`

```
Usage: fieldkit gmail sync [OPTIONS]

  Sync Gmail → SQLite. Incremental by default; resumes from the last sync. Use
  --full to re-sync all messages. Exits 2 when interactive Google OAuth
  consent is required but stdin is not a TTY. Run interactively and open the
  printed URL manually; no browser is launched (open_browser=False).

Options:
  --db TEXT                     Path to SQLite DB  [default: (dynamic)]
  --max-messages INTEGER RANGE  Stop after N messages (0=all)  [x>=0]
  --full                        Force a resumable full-mailbox refresh.
  --since YYYY-MM-DD            Upsert messages on or after a UTC date.
  --json                        Emit the sync outcome as JSON.
  -h, --help                    Show this message and exit.
```

## `fieldkit pursuit`

```
Usage: fieldkit pursuit [OPTIONS] [COMMAND] [ARGS]...

  Pursuit file management — create, audit, advance, archive, rename, and
  forecast pursuits.

Options:
  -h, --help  Show this message and exit.

Commands:
  advance       Evaluate the stage gate for a pursuit and advance it if...
  archive       Move closed or discarded pursuit files to...
  audit         Validate pursuit frontmatter compliance and timeline risks.
  create        Scaffold a new pursuit file with compliant frontmatter.
  forecast      Weighted pipeline forecast with scenario analysis.
  health        Show pipeline health — risk-ranked table of active pursuits.
  projects      Show delivery project health — zombie, expiring, and...
  rename        Rename a pursuit file and update all watcher state keys.
  repair-dates  Repair pursuit files whose last-transition date was...
```

### `fieldkit pursuit advance`

```
Usage: fieldkit pursuit advance [OPTIONS] [PURSUIT_SPEC]

  Evaluate the stage gate for a pursuit and advance it if the gate passes.

  PURSUIT_SPEC is a full path or '<account>/<slug>' shorthand, e.g.:

      fieldkit pursuit advance acme/deal-slug

  Alternatively, use --account and --name together:

      fieldkit pursuit advance --account acme --name deal-slug

  Checks the ratified current policy for the transition to the next stage (or
  --to STAGE), prints the decision, and updates the file in-place if the
  policy passes.

  Former score-dependent transitions remain pending until native ClosePlan
  policy is ratified. Use --override REASON to advance despite a pending gate.
  Use --dry-run to see what would happen without writing any changes.

  Exit codes:   0 — policy passed and advancement written (or --dry-run)   1 —
  policy pending (no override provided)   3 — data error (file unreadable,
  unknown stage, etc.)

Options:
  -a, --account ACCOUNT  Account slug (use with --name as an alternative to
                         positional PURSUIT_SPEC).
  -n, --name NAME        Pursuit slug (use with --account as an alternative to
                         positional PURSUIT_SPEC).
  --to STAGE             Target stage (default: next in sequence).
  --override REASON      Override a pending gate with this reason and advance
                         anyway.
  --dry-run              Print gate evaluation without modifying the pursuit
                         file.
  --json                 Emit the gate verdict as JSON.
  -h, --help             Show this message and exit.
```

### `fieldkit pursuit archive`

```
Usage: fieldkit pursuit archive [OPTIONS]

  Move closed or discarded pursuit files to accounts/<account>/archive/.

  Either pass --name to archive a single pursuit, or --all-closed to batch-
  archive every pursuit in the closed-won / closed-lost / won-lost stage.

  Exit codes: 0 success; 1 partial when an archive mutation fails; 3 missing
  account data or pursuit file.

Options:
  -a, --account TEXT  Account slug (e.g. acme-corp).  [required]
  -n, --name TEXT     Pursuit slug to archive (without .md).
  --all-closed        Archive all pursuits with stage closed-won, closed-lost,
                      or won-lost.
  --dry-run           Show what would be archived without moving files.
  --json              Emit the archive outcome as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit audit`

```
Usage: fieldkit pursuit audit [OPTIONS] ACCOUNT

  Validate pursuit frontmatter compliance and timeline risks.

  Optionally pass --account ACCOUNT (or -a ACCOUNT) to limit the audit to a
  single account directory. The bare positional ACCOUNT form is deprecated.

  Scans all pursuit files under the configured data root, reports errors and
  warnings, and optionally applies safe auto-corrections.

  Exit codes:   0 — all files compliant   1 — one or more files have errors or
  warnings   3 — data error (config missing, accounts directory not found)

Options:
  -a, --account TEXT  Limit audit to a single account directory name.
  --fix               Auto-correct common frontmatter issues (hyphenated SF
                      fields, legacy fields).
  --dry-run           Preview --fix corrections without writing pursuit files
                      or reports.
  -o, --output TEXT   Write report to this path (default: <data-
                      root>/accounts/.audit/pursuit-compliance-YYYY-MM-DD.md).
  --check-yaml        Scan pursuit files for duplicate YAML frontmatter keys.
                      Exits 1 if any found.
  --json              Machine-readable JSON output (suppresses table and
                      report file write).
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit create`

```
Usage: fieldkit pursuit create [OPTIONS]

  Scaffold a new pursuit file with compliant frontmatter.

Options:
  -a, --account TEXT  Account directory name (e.g. acme, globalpay).
                      [required]
  -n, --name TEXT     Pursuit name or slug (will be slugified: 'New Deal' →
                      'new-deal').  [required]
  -t, --title TEXT    Human-readable title (defaults to slugified name).
  --stage TEXT        Initial pursuit stage.  [default: pre-pipeline]
  --sf-id TEXT        Salesforce opportunity ID (optional — can be set later
                      with fieldkit sf opportunity).
  --dry-run           Show what would be created without writing.
  --json              Emit the creation outcome as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit forecast`

```
Usage: fieldkit pursuit forecast [OPTIONS]

  Weighted pipeline forecast with scenario analysis.

  Reads pursuit frontmatter to compute three scenarios:   Commit    — closed-
  won + negotiate deals (high confidence)   Best-case — all active deals
  summed   Weighted  — probability-weighted by stage

  Stage weights: closed-won=100%, negotiate=75%, propose=50%, validate=25%,
  discover=10%, qualify=5%

  When -q/--quota is not provided, reads pipeline.quota.target from config
  automatically.

  Exit codes:   0 — forecast computed   3 — data error (config missing,
  accounts directory not found)

Options:
  -a, --account TEXT  Limit to a single account directory name.
  -q, --quota FLOAT   Quota target in USD (e.g. 2000000). Overrides config.
                      Shows gap-to-quota.
  --json              Machine-readable JSON output.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit health`

```
Usage: fieldkit pursuit health [OPTIONS]

  Show pipeline health — risk-ranked table of active pursuits.

  Scans all active (non-closed) pursuit files and ranks them by risk tier:
  HIGH   — overdue close date   MEDIUM — approaching close date at an early
  stage, or missing SF ID   LOW    — no immediate concerns

  Current qualification is reported as unavailable; historical local scores
  are not used as current risk evidence.

  Exit codes:   0 — valid report (default), or no high-risk items with
  --strict   1 — one or more high-risk items with --strict   3 — data error
  (config missing, accounts directory not found)

Options:
  -a, --account TEXT  Limit to a single account directory name.
  --include-prospect  Include prospect-stage pursuits (excluded by default;
                      pre-pipeline pursuits are always excluded, regardless of
                      this flag).
  --json              Emit results as JSON array.
  --strict            Exit 1 when HIGH-risk items are present; valid reports
                      exit 0 by default.
  --compact           Truncate output lines to 80 characters for narrow
                      terminals.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit projects`

```
Usage: fieldkit pursuit projects [OPTIONS]

  Show delivery project health — zombie, expiring, and active engagements.

  Scans all project files and classifies each by contract end date:   ZOMBIE
  — contract end date past and not completed   EXPIRING — contract ends within
  30 days   SOON     — contract ends within 90 days   ACTIVE   — contract end
  date > 90 days out   UNKNOWN  — no contract end date in frontmatter

  Exit codes:   0 — valid report (default), or no ZOMBIE/UNKNOWN projects with
  --strict   1 — one or more ZOMBIE/UNKNOWN projects with --strict   3 — data
  error (config missing, accounts directory not found)

Options:
  -a, --account TEXT  Limit to a single account directory name.
  --json              Emit results as JSON array.
  --strict            Exit 1 when ZOMBIE or UNKNOWN projects are present;
                      valid reports exit 0 by default.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit rename`

```
Usage: fieldkit pursuit rename [OPTIONS]

  Rename a pursuit file and update all watcher state keys.

  Renames accounts/<account>/pursuits/<from>.md to <to>.md and updates the
  slug keys in pursuit-stall-state.json and close-date-countdown-state.json.

  Run after a Salesforce opportunity name change to keep the local pursuit
  file slug aligned with the SF record name.

Options:
  -a, --account TEXT  Account slug (e.g. acme-corp).  [required]
  --from TEXT         Current pursuit slug (without .md).  [required]
  --to TEXT           New pursuit slug (without .md).  [required]
  --dry-run           Show what would change without making any modifications.
  --json              Emit the rename outcome as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit pursuit repair-dates`

```
Usage: fieldkit pursuit repair-dates [OPTIONS]

  Repair pursuit files whose last-transition date was incorrectly set to
  today.

  Identifies pursuits where ``last-transition`` equals today after a
  normalization-only stage pass. For each affected file the command recovers
  the real date via git log (falling back to file mtime) and either prints the
  proposal (dry-run) or patches the file in place (--apply).

  The command is idempotent: subsequent runs after --apply produce no changes.

  Examples:

      fieldkit pursuit repair-dates --dry-run
      fieldkit pursuit repair-dates --apply
      fieldkit pursuit repair-dates --account acme-corp --apply

  Exit codes: 0 success; 3 configuration missing or unknown account slug.

Options:
  --dry-run           Print proposed changes without writing (default; use
                      --apply to write).
  --apply             Write corrected last-transition dates to pursuit files.
  -v, --verbose       Show all files scanned, not just those with proposed
                      changes.
  -a, --account SLUG  Limit the repair sweep to a single account slug.
                      Default: all accounts.
  --json              Emit repair outcomes as JSON.
  -h, --help          Show this message and exit.
```

## `fieldkit shadowbot`

```
Usage: fieldkit shadowbot [OPTIONS] [COMMAND] [ARGS]...

  ShadowBot sales assistant — query subcommand. Auth: fieldkit auth shadowbot.

Options:
  -h, --help  Show this message and exit.

Commands:
  query  Send a prompt to ShadowBot and stream the response.
```


> **Auth:** ShadowBot uses silent Chrome-cookie OIDC auth (Linux; requires `fieldkit-cli[chrome-auth]`).
> - **Primary:** Chrome Default profile must be logged into your configured ShadowBot host.
> - **Fallback (Linux/SSH/headless):** `fieldkit auth shadowbot --refresh-token-file PATH`
>   Get JWT: Chrome DevTools → Network → filter `openid-connect/token` → Response → `refresh_token`
> - **Config override:** `shadowbot.chrome_cookies_path` in `~/.config/fieldkit/config.yaml`
> - **Session expiry:** determined by the configured identity provider; recover by logging in again.

### `fieldkit shadowbot query`

```
Usage: fieldkit shadowbot query [OPTIONS] [PROMPT]...

  Send a prompt to ShadowBot and stream the response.

  The prompt may be passed as positional arguments or piped via stdin when
  stdin is not a TTY:

    echo "What are my open opportunities?" | fieldkit shadowbot query

Options:
  --timeout SECS  Total query deadline in seconds (default: 300)
  --new           Start a new conversation thread (don't reuse previous
                  context)
  --json          Machine-readable JSON output.
  -h, --help      Show this message and exit.
```

## `fieldkit watch`

```
Usage: fieldkit watch [OPTIONS] [COMMAND] [ARGS]...

  Account health watchers.

  run, status, logs.

Options:
  -h, --help  Show this message and exit.

Commands:
  logs    Show recent watcher log files.
  run     Run a single watcher by NAME, or all watchers with --all.
  status  Show each watcher's last-run outcome and timestamp.
```

### `fieldkit watch logs`

```
Usage: fieldkit watch logs [OPTIONS] [WATCHER]

  Show recent watcher log files.

  WATCHER is an optional watcher name (e.g. backstory-health) to filter.
  Without arguments, lists log files for all watchers.

Options:
  -n, --tail INTEGER  Show only the last N lines of the most recent log file.
  --list              List recent log files without printing content.
  --json              Emit log selection and content as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit watch run`

```
Usage: fieldkit watch run [OPTIONS] [COMMAND] [ARGS]...

  Run a single watcher by NAME, or all watchers with --all.

Options:
  --all             Run all watchers in sequence and write the morning brief.
  --dry-run         (--all only) Pass --dry-run to each watcher; no files are
                    written.
  --install-cron    (--all only) Install a crontab entry that runs 'fieldkit
                    watch run --all'.
  --cron-time TEXT  (--all only) Cron schedule expression used with --install-
                    cron.  [default: 0 6 * * *]
  --force           (--all only) Run even if watchers already ran today
                    (bypass once-per-day guard).
  --allow-partial   (--all only) Exit 0 after a completed partial pass; fatal
                    failures still fail.
  -h, --help        Show this message and exit.

Commands:
  backstory-health      Backstory account health watcher — detects...
  close-date-countdown  Close-date countdown watcher with native...
  contract-expiry       Contract-expiry watcher — alerts at configurable...
  draft-queue           Draft-queue watcher — lists stale Gmail drafts...
  pursuit-stalls        Pursuit stall watcher — detects pursuits stuck in...
  slack-threads         Slack thread age watcher — detects unanswered...
  waiting-on-tracker    Waiting-on tracker — escalate TASKS.md items...
```

#### `fieldkit watch run backstory-health`

```
Usage: fieldkit watch run backstory-health [OPTIONS]

  Backstory account health watcher — detects engagement drops and writes
  alerts.

Options:
  --threshold INTEGER  Alert threshold for mean engagement_level. Override per
                       account in accounts.yaml via health_score_threshold.
                       [default: 60]
  --account KEY        Only check this account key (as defined in
                       accounts.yaml). Default: all.
  --dry-run            Check scores and log what would be written; do not
                       modify any files.
  --json               Emit the run outcome as JSON.
  -h, --help           Show this message and exit.
```

#### `fieldkit watch run close-date-countdown`

```
Usage: fieldkit watch run close-date-countdown [OPTIONS]

  Close-date countdown watcher with native qualification availability.

Options:
  --threshold-red INTEGER     Days-to-close ≤ this → red tier.  [default: 14]
  --threshold-yellow INTEGER  Days-to-close ≤ this → yellow tier.  [default:
                              30]
  --threshold-green INTEGER   Days-to-close ≤ this → green tier.  [default:
                              60]
  --account KEY               Only check this account key.
  --dry-run                   Log what would be written; do not modify any
                              files.
  --json                      Emit the run outcome as JSON.
  -h, --help                  Show this message and exit.
```

#### `fieldkit watch run contract-expiry`

```
Usage: fieldkit watch run contract-expiry [OPTIONS]

  Contract-expiry watcher — alerts at configurable thresholds (default
  60/30/14 days).

  Scans accounts/*/pursuits/*.md for sf_close_date values and fires tiered
  alerts. Past-date pursuits are flagged EXPIRED. Thresholds are encoded in
  the suppression state key so changing them between runs causes re-alerting
  at the new boundaries.

  Example with custom thresholds:

      fieldkit watch run contract-expiry --threshold-notice 90 --threshold-
      warning 45 --threshold-critical 7

Options:
  --account KEY                 Only check this account key.
  --dry-run                     Log what would be written; do not modify any
                                files.
  --threshold-notice INTEGER    Days before expiry for the notice (yellow)
                                tier.  [default: 60]
  --threshold-warning INTEGER   Days before expiry for the warning (orange)
                                tier.  [default: 30]
  --threshold-critical INTEGER  Days before expiry for the critical (red)
                                tier. Must satisfy 0 < critical < warning <
                                notice. Custom thresholds must be used
                                consistently across runs — changing them
                                invalidates prior suppression state and causes
                                re-alerting. fieldkit watch run --all always
                                uses default thresholds (60/30/14).  [default:
                                14]
  --json                        Emit the run outcome as JSON.
  -h, --help                    Show this message and exit.
```

#### `fieldkit watch run draft-queue`

```
Usage: fieldkit watch run draft-queue [OPTIONS]

  Draft-queue watcher — lists stale Gmail drafts (>24h) and writes alerts.

Options:
  --dry-run           Log what would be written; do not call MCP or modify any
                      files.
  -a, --account TEXT  Limit draft-queue check to a single account slug.
  --json              Emit the run outcome as JSON.
  -h, --help          Show this message and exit.
```

#### `fieldkit watch run pursuit-stalls`

```
Usage: fieldkit watch run pursuit-stalls [OPTIONS] [COMMAND] [ARGS]...

  Pursuit stall watcher — detects pursuits stuck in the same stage for too
  long.

  Run without a subcommand to scan all pursuits and write alerts.

  Subcommands:   ack <account>/<pursuit>  Snooze re-alerting for a pursuit for
  7 days.

Options:
  --threshold INTEGER  Global stall threshold in days. Override per account in
                       accounts.yaml via stall_threshold_days.  [default: 14]
  --account KEY        Only check this account key (as defined in
                       accounts.yaml). Default: all.
  --dry-run            Check stalls and log what would be written; do not
                       modify any files.
  --force              Run even if already ran today (bypass once-per-day
                       guard). Used by run --all --force.
  --verbose            Show which pursuits were skipped and why (terminal
                       stage, missing last-transition, etc.).
  --scrub-duplicates   Deduplicate pursuit-stall-alerts.md in-place. One-time
                       operator action — keeps first occurrence of each alert
                       per date section.
  -h, --help           Show this message and exit.

Commands:
  ack  Snooze re-alerting for a pursuit for N days (default: 7).
```

##### `fieldkit watch run pursuit-stalls ack`

```
Usage: fieldkit watch run pursuit-stalls ack [OPTIONS] PURSUIT_KEY

  Snooze re-alerting for a pursuit for N days (default: 7).

  PURSUIT_KEY must be in the form ``<account>/<pursuit-slug>`` — the same key
  shown in stall alert headings, e.g. ``acme-corp/virtualization-deal``.

  The snooze is cleared automatically when the pursuit's stage changes. State
  tracking continues while snoozed; only alert writes are suppressed.

Options:
  --days INTEGER  Snooze duration in days.  [default: 7]
  --json          Emit the snooze outcome as JSON.
  -h, --help      Show this message and exit.
```

#### `fieldkit watch run slack-threads`

```
Usage: fieldkit watch run slack-threads [OPTIONS]

  Slack thread age watcher — detects unanswered account-related threads.

Options:
  --threshold-hours INTEGER    Alert threshold in hours. Threads older than
                               this from a non-self sender are flagged.
                               [default: 48]
  --account KEY                Only check this account key (as defined in
                               accounts.yaml). Default: all.
  --limit INTEGER              Total number of Slack messages to fetch per
                               account query. Use --limit-per-account to cap
                               results per account independently.  [default:
                               50]
  --limit-per-account INTEGER  Cap Slack search results per account (default:
                               no per-account limit). When set, each account
                               fetches at most this many results regardless of
                               --limit. Useful when active accounts consume
                               the full --limit quota.
  --dry-run                    Run checks and log what would be written; do
                               not modify any files.
  --json                       Emit the run outcome as JSON.
  -h, --help                   Show this message and exit.
```

#### `fieldkit watch run waiting-on-tracker`

```
Usage: fieldkit watch run waiting-on-tracker [OPTIONS]

  Waiting-on tracker — escalate TASKS.md items silent beyond threshold.

Options:
  --threshold INTEGER  Days of silence before an item triggers an escalation
                       alert.  [default: 7]
  --dry-run            Check items and log what would be written; do not
                       modify any files.
  --json               Emit the run outcome as JSON.
  -h, --help           Show this message and exit.
```

### `fieldkit watch status`

```
Usage: fieldkit watch status [OPTIONS]

  Show each watcher's last-run outcome and timestamp.

  Reads watcher-run-status.json (written by every watcher on completion).

Options:
  --json      Emit the watcher status table as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit ingest`

```
Usage: fieldkit ingest [OPTIONS] [COMMAND] [ARGS]...

  Source provenance and idempotent ingestion pipelines.

Options:
  -h, --help  Show this message and exit.

Commands:
  backfill   Find notes missing provenance
  discover   Discover new pipeline sources
  promote    Promote meeting action items
  reprocess  Re-run stored artifacts
  route      Route meetings to accounts
  run        Execute an ingestion pipeline
  status     Show pipeline status
```

### `fieldkit ingest backfill`

```
Usage: fieldkit ingest backfill [OPTIONS]

  Scan vault meeting notes for files without provenance stamps (source_id).

  This command is read-only — it never writes to any file.

  Exit codes: 0 success; 1 accounts directory missing; 3 unknown account slug.

Options:
  --dry-run           Accepted for interface consistency; has no effect
                      (command is always read-only).
  -a, --account SLUG  Limit the scan to a single account slug. Default: all
                      accounts.
  --json              Emit the candidate list as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit ingest discover`

```
Usage: fieldkit ingest discover [OPTIONS]

  Discover pipeline sources and register them.

Options:
  --pipeline PIPELINE_ID  Pipeline ID to inspect.  [default: transcript-
                          ingest]
  --dry-run               Preview what would be registered without modifying
                          pipeline.db.
  --limit N               Maximum number of Gmail messages to scan (most-
                          recent first).
  --include-latest        Include the newest ambient session, for use after
                          the recorder has stopped.
  -a, --account TEXT      Limit discovery to a single account slug.
  --json                  Emit the discovery result as JSON.
  -h, --help              Show this message and exit.
```

### `fieldkit ingest promote`

```
Usage: fieldkit ingest promote [OPTIONS] [MEETING_FILE]

  Interactively promote meeting action items to TASKS.md.

  Review each action item from a processed meeting note and choose:

    m  →  add to Active (it's your task)
    w  →  add to Waiting On (you're waiting on someone else)
    s  →  skip (delivery noise, not relevant to you)
    q  →  quit and save progress

  Examples:
    fieldkit ingest promote fieldkit-data/accounts/<acct>/meetings/2026-05-13-*.md
    fieldkit ingest promote --recent 5
    fieldkit ingest promote --recent 10 --account <acct>

Options:
  -r, --recent N      Promote from the N most recently written meeting notes
                      (omit FILE).
  -a, --account TEXT  Filter --recent to a specific account slug.
  -h, --help
```

### `fieldkit ingest reprocess`

```
Usage: fieldkit ingest reprocess [OPTIONS]

  Re-run artifacts through an updated pipeline version.

  Artifacts carry no account column, but each was written into its account's
  directory once routing decided, so --account recovers the scope from the
  stored content path.

  Exit codes: 0 success; 1 pipeline/version guard; 3 invalid selectors or
  account slug.

Options:
  --pipeline PIPELINE_ID  Pipeline ID to reprocess (e.g. transcript-ingest).
                          [required]
  --from-version VERSION  Reprocess artifacts produced by this pipeline
                          version.
  --force                 Reprocess matching artifacts regardless of stored
                          pipeline version.
  --dry-run               Preview what would be reprocessed without writing
                          anything.
  --interactive           Prompt y/n/q for each artifact before reprocessing.
  --limit N               Maximum number of artifacts to reprocess.
  -a, --account SLUG      Limit to artifacts under accounts/<slug>/. Default:
                          all accounts.
  --json                  Emit ordered batch outcomes as JSON.
  -h, --help              Show this message and exit.
```

### `fieldkit ingest route`

```
Usage: fieldkit ingest route [OPTIONS]

  Route meetings from accounts/unknown/ to correct account directories.

  Uses filename-prefix matching first (e.g. acme-corp-2026-05-01-*.md), then
  falls back to title-keyword matching from accounts.yaml.

  Files with ``reviewed-unmatched: true`` or ``ambiguous-match`` in their
  frontmatter are skipped (idempotency guard).

  Results written to frontmatter:
    - Single match: moves file, updates ``account`` field
    - Multiple matches: writes ``ambiguous-match: [acct1, acct2]``
    - No match: writes ``reviewed-unmatched: true``

  When the matchers cannot decide, resolve the leftover by hand:

      fieldkit ingest route --file 2026-06-18-quarterly.md --force-account acme-corp

  A force applies to one named file. It deliberately overrides the guards
  above, since those mark precisely the files that need manual resolution.

  Exit codes: 0 success; 3 unknown account slug, missing file, or
  --file/--force-account used without the other; 1 partial when a batch
  filesystem mutation fails after processing began.

Options:
  --dry-run             Report what would be moved without making changes.
  --data-root PATH      Override data root (default: from config).
  --file NAME           Single file in accounts/unknown/meetings/ to act on.
                        Required with --force-account.
  --force-account SLUG  Assign --file to this account, skipping the matchers.
                        Requires --file.
  --json                Emit the routing summary as JSON.
  -h, --help            Show this message and exit.
```

### `fieldkit ingest run`

```
Usage: fieldkit ingest run [OPTIONS]

  Execute an ingestion pipeline.

Options:
  --pipeline PIPELINE_ID  Pipeline ID to run (e.g. transcript-ingest).
                          [required]
  --dry-run               Preview what would run without writing anything.
  --limit N               Maximum number of sources to process.
  --interactive           Prompt y/n/q for each source before processing.
  --json                  Emit ordered batch outcomes as JSON.
  -h, --help              Show this message and exit.
```

### `fieldkit ingest status`

```
Usage: fieldkit ingest status [OPTIONS]

  List registered pipelines and their status.

  If pipeline.db exists, also shows source and artifact row counts. If
  count=0, run 'fieldkit ingest discover' to register sources.

Options:
  -a, --account TEXT  Filter status to a single account slug.
  --json              Emit the pipeline table as JSON.
  -h, --help          Show this message and exit.
```

## `fieldkit sync`

```
Usage: fieldkit sync [OPTIONS]

  Run the full fieldkit data pipeline in the correct order.

  Executes all data refresh steps idempotently: gmail sync, ingest, watchers.
  Step failures are non-fatal — the pipeline continues and a summary is
  printed at the end.

  Use --verbose to show the last 100 lines of each subprocess stream after its
  summary line, enabling in-terminal failure diagnosis without re-running
  individual watchers or flooding the terminal.

  Exit codes:   0 — all steps succeeded (or --dry-run)   1 — one or more steps
  failed

Options:
  --quick             Skip gmail sync phase (use cached data); run ingest +
                      watchers only.
  --sf                Include Salesforce listview sync (weekly mode).
  --dry-run           Show what would run without executing any steps.
  -a, --account NAME  Scope gmail and watcher steps to one account.
  --verbose           Print the last 100 lines of subprocess stdout/stderr
                      after each step's summary line. Useful for diagnosing
                      failures without re-running individual watchers.
  --json              Emit the per-step results as JSON.
  -h, --help          Show this message and exit.
```

## `fieldkit issue`

```
Usage: fieldkit issue [OPTIONS] [COMMAND] [ARGS]...

  GitHub Issues tracker — create bugs and enhancement requests.

  fieldkit-created issues use the public ``fieldkit-<number>`` identifier;
  their ``bug`` or ``enhancement`` label records the issue type.

Options:
  -h, --help  Show this message and exit.

Commands:
  board           Show a kanban-style terminal board of open issues.
  close           Close an issue (mark as closed or wont-fix).
  create          Create a new bug or enhancement request.
  edit            Edit issue metadata (title, body, severity, module).
  fix             Mark an issue as fixed — code committed, awaiting...
  link            Link an issue to a GitHub milestone, for example M001...
  list            List issues.
  note            Add a note to an issue without changing its status.
  plan            Mark an issue as planned (open → planned).
  reopen          Reopen a closed or wont-fix issue.
  show            Show details for a specific issue.
  sync-milestone  Bulk-advance issues linked to a GitHub milestone based...
```

### `fieldkit issue board`

```
Usage: fieldkit issue board [OPTIONS]

  Show a kanban-style terminal board of open issues.

Options:
  --json      Machine-readable JSON output.
  -h, --help  Show this message and exit.
```

### `fieldkit issue close`

```
Usage: fieldkit issue close [OPTIONS] ISSUE_ID

  Close an issue (mark as closed or wont-fix).

  Issues should be marked fixed first (`fieldkit issue fix <ID>`) and only
  closed after operator verification. Use --skip-verify to override.

Options:
  --wont-fix     Mark as won't fix instead of closed.
  --skip-verify  Close even if the issue has not been marked fixed first.
  --note TEXT    Attach a note (e.g. verification results, reason for wont-
                 fix).
  --json         Machine-readable JSON output.
  -h, --help     Show this message and exit.
```

### `fieldkit issue create`

```
Usage: fieldkit issue create [OPTIONS]

  Create a new bug or enhancement request.

Options:
  --type [bug|enhancement]        Issue type.  [required]
  --title TEXT                    Short title (one sentence).  [required]
  --body TEXT                     Detailed description. Markdown supported.
  --severity [low|medium|high|critical]
                                  Severity level (bugs) or impact
                                  (enhancements).  [default: medium]
  --module TEXT                   Affected module (sf, gmail, ingest, etc.).
                                  [default: other]
  --source TEXT                   Who raised this (agent name, user, etc.).
                                  [default: unknown]
  --json                          Machine-readable JSON output.
  -h, --help                      Show this message and exit.
```

### `fieldkit issue edit`

```
Usage: fieldkit issue edit [OPTIONS] ISSUE_ID

  Edit issue metadata (title, body, severity, module).

Options:
  --title TEXT                    New title.
  --body TEXT                     Replace body text.
  --severity [low|medium|high|critical]
  --module TEXT                   Affected module.
  --json                          Machine-readable JSON output.
  -h, --help                      Show this message and exit.
```

### `fieldkit issue fix`

```
Usage: fieldkit issue fix [OPTIONS] ISSUE_ID

  Mark an issue as fixed — code committed, awaiting verification (open|planned
  → fixed).

Options:
  --commit TEXT  Git commit SHA to record with this fix.
  --note TEXT    Attach a note (e.g. test results) to this transition.
  --json         Machine-readable JSON output.
  -h, --help     Show this message and exit.
```

### `fieldkit issue link`

```
Usage: fieldkit issue link [OPTIONS] ISSUE_ID MILESTONE_ID

  Link an issue to a GitHub milestone, for example M001 or 'Sprint 3'.

  Creates the milestone on GitHub if it does not already exist. Once linked,
  `fieldkit issue sync-milestone` can bulk-advance statuses.

Options:
  --note TEXT  Attach a note to record the association.
  --json       Machine-readable JSON output.
  -h, --help   Show this message and exit.
```

### `fieldkit issue list`

```
Usage: fieldkit issue list [OPTIONS]

  List issues. Default shows only open issues; use --all to see everything.

Options:
  --status [open|planned|fixed|closed|wont-fix|all]
                                  Filter by status. Default: open.  [default:
                                  open]
  --all                           Show all issues regardless of status
                                  (shorthand for --status all).
  --type [bug|enhancement|all]    [default: all]
  --module TEXT                   Filter by module.
  --json                          Machine-readable JSON output.
  -h, --help                      Show this message and exit.
```

### `fieldkit issue note`

```
Usage: fieldkit issue note [OPTIONS] ISSUE_ID TEXT

  Add a note to an issue without changing its status.

  Example: fieldkit issue note <issue-id> "Verified on staging — all good."

Options:
  --json      Machine-readable JSON output.
  -h, --help  Show this message and exit.
```

### `fieldkit issue plan`

```
Usage: fieldkit issue plan [OPTIONS] ISSUE_ID

  Mark an issue as planned (open → planned).

Options:
  --note TEXT  Attach a note to this status transition.
  --json       Machine-readable JSON output.
  -h, --help   Show this message and exit.
```

### `fieldkit issue reopen`

```
Usage: fieldkit issue reopen [OPTIONS] ISSUE_ID

  Reopen a closed or wont-fix issue.

Options:
  --note TEXT  Attach a note explaining why this issue is being reopened.
  --json       Machine-readable JSON output.
  -h, --help   Show this message and exit.
```

### `fieldkit issue show`

```
Usage: fieldkit issue show [OPTIONS] ISSUE_ID

  Show details for a specific issue.

Options:
  --json      Machine-readable JSON output.
  -h, --help  Show this message and exit.
```

### `fieldkit issue sync-milestone`

```
Usage: fieldkit issue sync-milestone [OPTIONS] MILESTONE_ID

  Bulk-advance issues linked to a GitHub milestone based on milestone state.

  queued:    open → planned  (milestone was queued/added to roadmap)
  completed: planned → fixed (milestone was completed)

  Issues already in the target status, or further ahead, are skipped.

Options:
  --state [queued|completed]  queued → open issues become planned. completed →
                              planned issues become fixed.  [required]
  --commit TEXT               Git commit SHA to record when --state completed.
  --dry-run                   Show what would change without writing.
  --json                      Machine-readable JSON output.
  -h, --help                  Show this message and exit.
```

## `fieldkit skill`

```
Usage: fieldkit skill [OPTIONS] [COMMAND] [ARGS]...

  Skill discovery and inspection.

Options:
  -h, --help  Show this message and exit.

Commands:
  eval       Run static assertions and print behavioral eval cases for...
  install    Install skills into project-local paths for the selected AI...
  list       List all available skills with names and trigger descriptions.
  show       Show full details for a specific skill.
  variables  Show all available {{key}} template variables and their...
```

### `fieldkit skill eval`

```
Usage: fieldkit skill eval [OPTIONS] [NAMES]...

  Run static assertions and print behavioral eval cases for skill(s).

  Use --behavioral to run LLM-graded coverage evals against skill assertions.
  Use --calibrate to validate judge accuracy against gold-standard fixtures.
  Use --routing to evaluate utterance-to-skill selection against the full
  corpus.

Options:
  --all             Evaluate all skills.
  --failed          Show only skills with failing checks.
  --no-behavioral   Skip printing behavioral eval cases.
  --json            Machine-readable JSON output.
  --scaffold SKILL  Scaffold a starter evals.json for the named skill.
  --behavioral      Run LLM-graded behavioral evals.
  --calibrate       Run calibration fixtures to validate judge accuracy.
  --routing         Evaluate utterance routing against all skills.
  --limit N         Max number of skills to judge (behavioral mode).
  --skill NAME      Skill name to evaluate (repeatable; additive with
                    positional names).
  -h, --help        Show this message and exit.
```

### `fieldkit skill install`

```
Usage: fieldkit skill install [OPTIONS]

  Install skills into project-local paths for the selected AI tools.

  Scans the current directory for known AI tool config directories
  (.opencode/, .claude/, .cursor/, .gemini/) and presents an interactive
  multi-select prompt for tools and skills.

  Pass --tool and --skill to skip the interactive prompts (useful for CI).
  Pass --all to install every skill without selecting (useful for make
  install). Pass --global with an explicit OpenCode or Claude Code tool and
  handoffs or pickup skill to install it for the current user. Pass --prune to
  preview stale installer-owned skills, then add --confirm to remove them.
  Untracked local skills are always left untouched.

Options:
  --tool TOOL    Pre-select tool(s): opencode, claude-code, cursor, gemini
                 (repeatable). Skips the tool selection prompt.
  --skill SKILL  Pre-select a skill by name (repeatable). Skips the skill
                 selection prompt.
  --all          Install all skills without interactive selection.
  --global       Install handoffs or pickup into a registered OpenCode or
                 Claude Code user skill root.
  --dry-run      Preview install without writing files.
  --prune        Preview removal of stale installer-owned skills.
  --confirm      Apply removals selected by --prune.
  --force        Overwrite locally modified or untrusted skill targets.
  --json         Emit the installation result as JSON.
  -h, --help     Show this message and exit.
```

### `fieldkit skill list`

```
Usage: fieldkit skill list [OPTIONS]

  List all available skills with names and trigger descriptions.

Options:
  --json         Output as JSON.
  --group TEXT   Filter skills by group name.
  -v, --verbose  Show full skill descriptions.
  -h, --help     Show this message and exit.
```

### `fieldkit skill show`

```
Usage: fieldkit skill show [OPTIONS] NAME

  Show full details for a specific skill.

Options:
  --json      Output as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit skill variables`

```
Usage: fieldkit skill variables [OPTIONS] [SKILL_NAME]

  Show all available {{key}} template variables and their current values.

  Template variables are global — they apply to all skills. Passing SKILL_NAME
  is accepted but has no effect on the output.

Options:
  --json      Output as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit init`

```
Usage: fieldkit init [OPTIONS] [COMMAND] [ARGS]...

  First-run configuration wizard. Safe to re-run to update settings.

Options:
  --answers FILE  Initialize without prompts using answers from a YAML file.
  --minimal PATH  Create a generic offline workspace at PATH without prompts
                  or integrations.
  -h, --help      Show this message and exit.

Commands:
  migrate  Rename deprecated 'data_repo' config key to 'fieldkit_home'.
```

### `fieldkit init migrate`

```
Usage: fieldkit init migrate [OPTIONS]

  Rename deprecated 'data_repo' config key to 'fieldkit_home'.

  Reads ~/.config/fieldkit/config.yaml. If the deprecated 'data_repo' key
  exists and 'fieldkit_home' is absent, renames it in-place.

  A backup is saved as config.yaml.bak before any modification.

  Exit codes:   0 — migration done (or already migrated)   1 — config file not
  found or not writable

Options:
  --json      Emit the migration outcome as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit brief`

```
Usage: fieldkit brief [OPTIONS] [COMMAND] [ARGS]...

  Morning brief generation and retrieval.

  generate, open.

Options:
  -h, --help  Show this message and exit.

Commands:
  generate  Generate a dated morning brief.
  open      Open the most recent saved morning brief in the system...
```

### `fieldkit brief generate`

```
Usage: fieldkit brief generate [OPTIONS]

  Generate a dated morning brief.

  By default, aggregates watcher alerts (backstory, pursuit stalls, Slack,
  contract expiry, draft queue), calendar meetings, and a pipeline review into
  a single brief written to <fieldkit_home>/briefs/. Use --pipeline-only for
  just the pursuit/champion/decay pipeline review, without watcher alerts or
  calendar data.

Options:
  --date YYYY-MM-DD   Date to generate the brief for (default: today). Ignored
                      with --pipeline-only.
  --dry-run           Generate the brief and print to stdout; do not write any
                      files.
  -v, --verbose       Enable verbose/debug output.
  -a, --account TEXT  Limit brief to a single account slug.
  --pipeline-only     Generate only the pipeline-review brief (pursuit alerts,
                      champion/decay signals, tasks) without watcher-alert
                      aggregation or calendar meetings.
  --no-llm            (--pipeline-only only) Skip LLM synthesis; write pre-
                      computed sections only.
  --json              Emit the generation result as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit brief open`

```
Usage: fieldkit brief open [OPTIONS]

  Open the most recent saved morning brief in the system default viewer.

  Looks for the most recent brief file in the briefs/ directory under the
  configured data root. Use 'fieldkit brief generate' to generate a new one.

Options:
  --json      Emit the selected brief as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit pipeline`

```
Usage: fieldkit pipeline [OPTIONS] [COMMAND] [ARGS]...

  Generate the weekly pipeline review document.

  Running 'fieldkit pipeline' generates and prints the review. Use 'fieldkit
  pipeline open' to re-open the most recent saved review. Use 'fieldkit
  pipeline quota' to show the quota gap summary.

  The --no-llm flag skips LLM synthesis and works when placed anywhere:
    fieldkit pipeline --no-llm
    fieldkit pipeline open  (--no-llm not applicable to subcommands)

Options:
  --no-llm            Skip LLM synthesis; write deterministic table only.
  --data-root PATH    Override data root path (default: from fieldkit.config).
  -a, --account SLUG  Generate a review for one configured account.
  -h, --help          Show this message and exit.

Commands:
  open   Open the most recent saved pipeline review in the system default...
  quota  Show quota gap: Weighted pipeline / Closed-won / Quota target /...
```

### `fieldkit pipeline open`

```
Usage: fieldkit pipeline open [OPTIONS]

  Open the most recent saved pipeline review in the system default viewer.

  Looks for the most recent pipeline-review-*.md file in the briefs/
  directory. Use 'fieldkit pipeline' to generate a new one.

Options:
  --json              Emit the selected pipeline review as JSON.
  -a, --account SLUG  Open the newest review for one account.
  -h, --help          Show this message and exit.
```

### `fieldkit pipeline quota`

```
Usage: fieldkit pipeline quota [OPTIONS]

  Show quota gap: Weighted pipeline / Closed-won / Quota target / Gap.

  Reads quota target from ~/.config/fieldkit/config.yaml
  (pipeline.quota.target). Configure with: fieldkit pipeline quota --set
  <target> --period <period>

  Default (--source pursuits) labels closed-won as configured-pursuit-scoped
  and suppresses the gap. Pass --source sf for a live, territory-scoped
  attainment gap.

Options:
  --set INTEGER           Set the quota target in USD (e.g. 5000000). Requires
                          --period.
  --period TEXT           Quota period label (e.g. 2026-H2). Required with
                          --set.
  --data-root PATH        Override data root path (default: from
                          fieldkit.config).
  --json                  Machine-readable JSON output.
  -a, --account TEXT      Filter quota to a single account slug.
  --source [pursuits|sf]  Source for closed-won figures. 'pursuits' (default)
                          uses local pursuit files and does not compute an
                          attainment gap (pursuit scope is not comparable to a
                          full-book quota). 'sf' pulls live territory-scoped
                          closed-won from Salesforce and computes a real gap.
                          [default: pursuits]
  -h, --help              Show this message and exit.
```

## `fieldkit version`

```
Usage: fieldkit version [OPTIONS]

  Print version, Python, and platform info.

  Use --features to see active capabilities (CLI groups, skills, services).
  Use --json for machine-readable output.

Options:
  -f, --features  Show active capabilities.
  --json          Machine-readable JSON output.
  -h, --help      Show this message and exit.
```

## `fieldkit commands`

```
Usage: fieldkit commands [OPTIONS]

  List every leaf command with its summary, write class, and account scope.

  The registry agents route on: full command name, argument/flag surface,
  write classification (read-only / workspace / external), and whether
  `--account` scoping is supported. Use `--json` for the machine-readable
  form; the default table is for humans.

  The Account column reads `filter` (optional, narrows a sweep across all
  accounts) or `selector` (names the single account acted on — omitting it
  does not widen the command). Blank means the command has no `--account`.

  `write_class` carries its provenance in `write_class_source`. "declared"
  means the command states what it writes via `@declare_write` and is safe to
  route on; "inferred" means it was guessed from flag presence, which
  describes the flags offered rather than what the command touches. The table
  marks inferred rows with a trailing `?`.

Options:
  --json      Emit the full registry as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit companion`

```
Usage: fieldkit companion [OPTIONS] COMMAND [ARGS]...

  Agent companion loop — feed, allowed, run.

Options:
  -h, --help  Show this message and exit.

Commands:
  allowed            Answer whether COMMAND is permitted at the...
  feed               Emit new attention items from watchers, alert files,...
  loop               Run one poll→decide→gate→act→journal pass over the...
  prune              Preview or retire old pending proposals in a bounded...
  reconcile-invalid  Replace fresh invalid-command proposals with...
  run                Gate-check COMMAND, execute it, and journal the...
```

### `fieldkit companion allowed`

```
Usage: fieldkit companion allowed [OPTIONS] [COMMAND]...

  Answer whether COMMAND is permitted at the configured tier.

  Exit 0 = permitted; exit 3 = denied (EXIT_DATA — denial is permanent, so
  exit 1 'retry may help' would mislead orchestrators). A malformed
  act_allowlist entry is also a denial: it is checked here before the per-
  invocation question, so a typo'd or non-previewable allowlist entry is
  caught the first time anyone asks what's permitted, not silently at
  execution time.

  --json goes before the ``--`` separator; anything after ``--`` belongs to
  the command being gate-checked, including its own --json.

  Example: fieldkit companion allowed -- pursuit advance acme/deal --dry-run

Options:
  --json      Machine-readable JSON output.
  -h, --help  Show this message and exit.
```

### `fieldkit companion feed`

```
Usage: fieldkit companion feed [OPTIONS]

  Emit new attention items from watchers, alert files, and TASKS.md.

  Default output is JSON lines. Repeated polls return only items not yet
  delivered (cursor in <fieldkit_data>/companion-cursor.json); --all bypasses
  the cursor without advancing it. Items on a cooldown or awaiting review in
  the outbox are withheld until the cooldown expires or the proposal file is
  deleted.

  Exit codes: 0 — success (with or without items); 2 — config absent; 3 —
  malformed upstream state (unparseable status JSON).

Options:
  --json          Emit JSON lines (one item per line).
  --markdown      Render as markdown for human reading.
  --all           Ignore the cursor; show every current item.
  --account TEXT  Filter to a single account slug.
  -h, --help      Show this message and exit.
```

### `fieldkit companion loop`

```
Usage: fieldkit companion loop [OPTIONS]

  Run one poll→decide→gate→act→journal pass over the attention feed.

  The headless companion brain: polls the feed, gathers deterministic context,
  and may refine proposals through the configured Vertex-routed LLM. Honors
  ``companion.tier`` (read/propose/act); ``--tier`` can only LOWER it. At
  propose tier, writes one companion-outbox proposal per item without running
  its candidate command. At act tier, the candidate still must pass the exact
  configured allowlist before execution. Read tier and ``NO_LLM=1`` retain
  deterministic behavior.

  Exit codes: 0 — success; 1 — partial (a best-effort context read failed); 3
  — malformed feed input or an invalid/escalating --tier.

Options:
  --once       Run a single pass (the only supported mode).
  --dry-run    Compute proposals without writing, running, or journaling.
  --tier TEXT  Lower the effective tier for this run (may not exceed the
               configured tier).
  --json       Emit the LoopResult as JSON.
  -h, --help   Show this message and exit.
```

### `fieldkit companion prune`

```
Usage: fieldkit companion prune [OPTIONS]

  Preview or retire old pending proposals in a bounded batch.

  Valid version-1 proposals are selected oldest first. Legacy, malformed, and
  execution-marker proposals are preserved. Confirmed expiry keeps the
  condition suppressed for 24 hours and records lifecycle evidence.

  Exit codes: 0 — success; 2 — config absent; 3 — invalid arguments.

Options:
  --older-than TEXT      Select proposals at least this old (Nd).  [default:
                         14d]
  --limit INTEGER RANGE  Maximum proposals to retire.  [default: 25;
                         1<=x<=100]
  --confirm              Retire the selected proposals; otherwise preview
                         only.
  --dry-run              Explicitly preview without retiring proposals.
  --json                 Emit one machine-readable result object.
  -h, --help             Show this message and exit.
```

### `fieldkit companion reconcile-invalid`

```
Usage: fieldkit companion reconcile-invalid [OPTIONS]

  Replace fresh invalid-command proposals with deterministic baselines.

Options:
  --limit INTEGER RANGE  Maximum proposals to replace.  [default: 25;
                         1<=x<=100]
  --confirm              Atomically replace the selected proposals.
  --dry-run              Explicitly preview without replacing proposals.
  --json                 Emit one machine-readable result object.
  -h, --help             Show this message and exit.
```

### `fieldkit companion run`

```
Usage: fieldkit companion run [OPTIONS] [COMMAND]...

  Gate-check COMMAND, execute it, and journal the outcome — one call.

  Exits with the subprocess exit code, or 3 on gate denial (the denial is
  journaled too, so refusals appear in the audit trail).

  Example: fieldkit companion run --item-id abc123 -- pursuit health --json

Options:
  --item-id TEXT  Attention item this action addresses (journaled).
  -h, --help      Show this message and exit.
```

## `fieldkit completion`

```
Usage: fieldkit completion [OPTIONS] [bash|zsh|fish] [COMMAND] [ARGS]...

  Output shell completion script.

  Supported shells: bash, zsh, fish.

  Quick setup:
      eval "$(fieldkit completion bash)"   # add to ~/.bashrc
      eval "$(fieldkit completion zsh)"    # add to ~/.zshrc
      fieldkit completion fish | source    # fish

  Uses Click's built-in completion mechanism. The generated script sets
  _FIELDKIT_COMPLETE=<shell>_source when sourced, which triggers Click to
  output completions on subsequent invocations.

Options:
  -h, --help  Show this message and exit.
```

## `fieldkit contact`

```
Usage: fieldkit contact [OPTIONS] [COMMAND] [ARGS]...

  Contact lookup, cache, enrichment, and reporting.

Options:
  -h, --help  Show this message and exit.

Commands:
  enrich  Discover contacts, merge web-search results, or run the...
  find    Look up a contact by email or name and display a unified profile.
  list    List people from the cached people index (does not rebuild the...
  report  Generate a markdown coverage report from enriched contacts.
```

### `fieldkit contact enrich`

```
Usage: fieldkit contact enrich [OPTIONS]

  Discover contacts, merge web-search results, or run the enrichment pipeline.

  With no flags, runs the full enrichment pipeline (requires contacts-raw.json
  to already exist — run with --discover first).

Options:
  --discover      Scan vault files and Gmail cache for contacts
  --apply-web     Apply web-search-results.json to raw contacts
  --account SLUG  Restrict the operation to this account slug
  --json          Emit JSON output instead of a summary
  -h, --help      Show this message and exit.
```

### `fieldkit contact find`

```
Usage: fieldkit contact find [OPTIONS] QUERY

  Look up a contact by email or name and display a unified profile.

Options:
  --db PATH       Path to gmail.db (defaults to configured gmail_db path)
  --affiliations  Also scan pursuit files for matching stakeholder tables
  --json          Emit JSON output instead of human-readable card
  -h, --help      Show this message and exit.
```

### `fieldkit contact list`

```
Usage: fieldkit contact list [OPTIONS]

  List people from the cached people index (does not rebuild the cache).

Options:
  --account SLUG  Restrict to this account slug
  --limit N       Return at most N people
  --json          Emit JSON output instead of a table
  -h, --help      Show this message and exit.
```

### `fieldkit contact report`

```
Usage: fieldkit contact report [OPTIONS]

  Generate a markdown coverage report from enriched contacts.

Options:
  --account SLUG  Restrict the report to this account slug
  --json          Emit the report as JSON ({"report": <markdown>})
  -h, --help      Show this message and exit.
```

## `fieldkit doctor`

```
Usage: fieldkit doctor [OPTIONS] [COMMAND] [ARGS]...

  Check the health of every configured service, or a single one.

  Exits 0 when enabled services are healthy, 2 for auth, and 3 for invalid
  configuration.

Options:
  --json      Emit results as JSON.
  -h, --help  Show this message and exit.

Commands:
  gmail      Check the local gmail.db cache for integrity.
  google     Check Google OAuth credential health (gmail + docs + drive).
  sf         Check Salesforce session health.
  shadowbot  Check ShadowBot OIDC token health.
```

### `fieldkit doctor gmail`

```
Usage: fieldkit doctor gmail [OPTIONS]

  Check the local gmail.db cache for integrity.

  Exits 0 when the database is reachable and structurally sound, 2 otherwise.

Options:
  --db PATH   Path to gmail.db (defaults to the configured Gmail database).
  --json      Emit result as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit doctor google`

```
Usage: fieldkit doctor google [OPTIONS]

  Check Google OAuth credential health (gmail + docs + drive).

  Exits 0 when the token is valid or was refreshed, 2 when not configured or
  expired.

Options:
  --json      Emit result as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit doctor sf`

```
Usage: fieldkit doctor sf [OPTIONS]

  Check Salesforce session health.

  Exits 0 when the session is active, 2 when expired or not configured.

Options:
  --json      Emit result as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit doctor shadowbot`

```
Usage: fieldkit doctor shadowbot [OPTIONS]

  Check ShadowBot OIDC token health.

  Exits 0 when a valid access token can be resolved, 2 when not configured or
  expired.

Options:
  --json      Emit result as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit golive`

```
Usage: fieldkit golive [OPTIONS] OPPORTUNITY

  Source a go-live revenue block from an opportunity's CPQ quote lines.

  OPPORTUNITY is a 15/18-character Salesforce id or a 5-12 digit Opportunity
  Number.

Options:
  --json      Emit deterministic sourced rows as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit gtask`

```
Usage: fieldkit gtask [OPTIONS] COMMAND [ARGS]...

  Preview and apply Google Tasks changes.

Options:
  -h, --help  Show this message and exit.

Commands:
  complete  Preview or complete TASK_ID.
  create    Preview or create a task named TITLE.
```

### `fieldkit gtask complete`

```
Usage: fieldkit gtask complete [OPTIONS] TASK_ID

  Preview or complete TASK_ID.

Options:
  --dry-run   Preview without writing (the default).
  --confirm   Complete the task.
  --json      Emit JSON only.
  -h, --help  Show this message and exit.
```

### `fieldkit gtask create`

```
Usage: fieldkit gtask create [OPTIONS] TITLE

  Preview or create a task named TITLE.

Options:
  --section [today|active]  [required]
  --account TEXT
  --due YYYY-MM-DD
  --dry-run                 Preview without writing (the default).
  --confirm                 Create the task.
  --json                    Emit JSON only.
  -h, --help                Show this message and exit.
```

## `fieldkit meeting`

```
Usage: fieldkit meeting [OPTIONS] [COMMAND] [ARGS]...

  Pursuit Workbook — Google Docs integration for pursuit documentation.

Options:
  -h, --help  Show this message and exit.

Commands:
  link  Create a Pursuit Workbook GDoc and write its ID to frontmatter.
  list  List all pursuits that have a linked Pursuit Workbook.
  note  Add a meeting note as a new tab in the Pursuit Workbook.
  open  Open the linked Pursuit Workbook in the default browser.
```

### `fieldkit meeting link`

```
Usage: fieldkit meeting link [OPTIONS] PURSUIT_FILE

  Create a Pursuit Workbook GDoc and write its ID to frontmatter.

  The workbook is a single pageless Google Doc with tabs:   Tab 1 (Overview):
  narrative, stakeholders, MEDDPICC, risks, scope   Additional tabs are added
  per meeting via 'fieldkit meeting note'.

Options:
  -o, --open  Open the workbook in the browser after creation.
  --json      Emit the workbook link result as JSON.
  -h, --help  Show this message and exit.
```

### `fieldkit meeting list`

```
Usage: fieldkit meeting list [OPTIONS]

  List all pursuits that have a linked Pursuit Workbook.

  Scans accounts/*/pursuits/*.md under the data root and prints one line per
  pursuit whose frontmatter contains a non-empty 'gdoc_workbook' field.

  Output format: <relative_path>  <doc_url>

  Exit codes: 0 success; 3 unknown account slug.

Options:
  -a, --account SLUG  Limit the listing to a single account slug. Default: all
                      accounts.
  --json              Emit the listing as JSON.
  -h, --help          Show this message and exit.
```

### `fieldkit meeting note`

```
Usage: fieldkit meeting note [OPTIONS] PURSUIT_FILE

  Add a meeting note as a new tab in the Pursuit Workbook.

Options:
  --title TEXT    Meeting title (used as tab name)
  --content TEXT  Meeting note content (reads from stdin if not provided)
  -o, --open      Open the workbook in the browser after adding the tab.
  --json          Emit the added-tab result as JSON.
  -h, --help      Show this message and exit.
```

### `fieldkit meeting open`

```
Usage: fieldkit meeting open [OPTIONS] PURSUIT_FILE

  Open the linked Pursuit Workbook in the default browser.

Options:
  --json      Emit the resolved workbook URL as JSON.
  -h, --help  Show this message and exit.
```

## `fieldkit web`

```
Usage: fieldkit web [OPTIONS] COMMAND [ARGS]...

  Local web dashboard — brief, pipeline, alerts, chat (PWA-installable).

Options:
  -h, --help  Show this message and exit.

Commands:
  serve  Run the web server (blocking; Ctrl-C to stop).
  token  Generate a bearer token and store it at...
```

### `fieldkit web serve`

```
Usage: fieldkit web serve [OPTIONS]

  Run the web server (blocking; Ctrl-C to stop).

  Serves the morning brief, pipeline health board, watcher alerts, and a
  fieldkit-context chat at http://<host>:<port>/. The page is installable as a
  PWA (add to home screen on mobile).

Options:
  --host TEXT        Bind address.  [default: 127.0.0.1]
  --port INTEGER     Bind port.  [default: 6096]
  --token-file PATH  Read a bearer token from this file and require it on all
                     API and write requests. The listener remains loopback-
                     only. Generate one with: fieldkit web token
  -h, --help         Show this message and exit.
```

### `fieldkit web token`

```
Usage: fieldkit web token [OPTIONS]

  Generate a bearer token and store it at ~/.config/fieldkit/web-token.

  Use with: fieldkit web serve --token-file ~/.config/fieldkit/web-token
  Clients must then send: Authorization: Bearer <token>

Options:
  --json      Machine-readable JSON output.
  -h, --help  Show this message and exit.
```

---

## Common patterns

### Update SF Next Steps
```bash
fieldkit sf set-next-steps <OPP_ID> "Next steps text here" --confirm
```

### Write any other approved SF field
```bash
fieldkit sf set-field --list-fields          # see what's allowed
fieldkit sf set-field <OPP_ID> Next_Steps__c "text" --confirm
fieldkit sf set-field <QUOTE_ID> Approval_Comments__c "justification" --sobject SBQQ__Quote__c --confirm
```

### Refresh SF auth
```bash
# 1. Get sid from Chrome DevTools → your my.salesforce.com host → Cookies → sid
fieldkit auth sf
fieldkit sf session-check
```

### Refresh ShadowBot auth
```bash
# Primary: Chrome Default profile must be logged into your configured ShadowBot host
fieldkit auth shadowbot   # checks auth status; auto-refreshes via Chrome cookies (Linux)

# Fallback (SSH/headless/macOS): inject refresh token from DevTools
# Chrome DevTools → Network → filter openid-connect/token → Response → refresh_token
fieldkit auth shadowbot --refresh-token-file PATH
fieldkit shadowbot query 'test query'
```

### Daily data refresh
```bash
fieldkit gmail sync
fieldkit gmail account-tags
fieldkit gmail enrich-pursuits
fieldkit sf listview
fieldkit watch run backstory-health
fieldkit watch run pursuit-stalls
fieldkit watch run slack-threads
fieldkit brief generate
```

### Pipeline review
```bash
fieldkit pursuit health
fieldkit pursuit forecast
fieldkit pursuit audit
```
