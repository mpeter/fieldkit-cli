# Refresh Gmail-derived account reports

Refresh the local Gmail cache and regenerate `gmail-intel.md` for one confirmed
account. This workflow reads Gmail and writes local state; it never sends email.

## Confirm scope and authorization

Require an exact configured account slug. If the request names no account or
matches more than one account, stop and ask the operator to choose.

Explain the effects before asking for approval:

- the live Gmail sync is mailbox-wide because its cache cannot be scoped to one
  account;
- account tagging writes associations to the local Gmail database for the
  selected account; and
- enrichment replaces only the selected account's `gmail-intel.md` and does not
  modify pursuit frontmatter.

Ask whether to use live Gmail or the existing cache. Obtain explicit approval
for the complete command sequence and its stated scope before executing it.

## Live Gmail refresh

Run the steps in order for the confirmed account:

```console
fieldkit doctor google --json
fieldkit gmail sync --since <YYYY-MM-DD> --max-messages <N> --json
```

Only after both commands exit 0, regenerate the selected account's local
associations and report:

```console
fieldkit gmail account-tags --account <account> --json
fieldkit gmail enrich-pursuits --account <account> --json
```

The Google health check can refresh an expired token. It reports credential
health without printing client secrets. Do not ask the operator to paste a
secret or token into the conversation.

The date and positive message limit are required operator-selected bounds for
this workflow. They still cover every matching mailbox message, not only the
selected account, and the resulting view can be incomplete for broader
analysis. Use an ordinary unbounded incremental or full sync only as a separate,
explicitly approved maintenance operation.

Each complete provider page and its checkpoint become one committed cache
generation. A fresh cache is not ready for queries until the first provider page
completes, including a valid empty result. If that first page fails, the sync is
non-passing and the cache remains not ready; do not run tagging or enrichment.
If a later page fails, previously published pages remain available but the
refresh is partial, so stop rather than mixing the prior generation with a new
report.

## Refresh from the existing cache

When the operator explicitly chooses cached data, verify the cache and skip the
network sync:

```console
fieldkit doctor gmail --json
fieldkit gmail account-tags --account <account> --json
fieldkit gmail enrich-pursuits --account <account> --json
```

Continue only when the cache health check exits 0. Call the result cached, not
fresh from Gmail.

## Import an explicitly selected legacy cache

An unmanaged cache is not adopted or upgraded by a read. Stop its writers,
preserve the source database and every sidecar, choose a fresh target under the
configured runtime-data root, and obtain approval for the local copy:

```console
fieldkit gmail import-cache --source <legacy-gmail.db> --db <fresh-managed-gmail.db> --json
```

The importer reads through an owner-private verified snapshot and leaves the
source database and its sidecars unchanged. The target must be a fresh target;
unknown schemas, changing or active source state, unsupported values, exceeded
resource bounds, and an existing target fail closed. A successful import creates
a new managed identity and a query-ready committed generation. It does not
establish when or how the legacy rows were originally committed.

Importing with `--db` does not select that target for subsequent commands or
change configuration. Those commands use the configured `gmail_db`, otherwise
`gmail.db` under the runtime-data root. Show the current selection and imported
target, obtain separate approval to update `gmail_db` through the existing
configuration workflow if needed, and verify the selected cache with Gmail
doctor before tagging or enrichment. Never assume a successful import switched
the report's inputs.

## Do not substitute the broader sync command

The top-level `fieldkit sync` command is not a people-index shortcut or a Gmail
refresh alias. It also runs transcript discovery and execution plus watchers;
its `--account` option does not scope the transcript pending queue. Use it only
when the operator separately requests and approves that broader workflow.

The people index has no standalone public refresh command. Do not promise a
people-index rebuild from the scoped Gmail sequence above.

## Fail closed and report exact status

Stop on any non-zero status. Do not continue with older tags or reports after a
failed prerequisite.

- Google doctor status 2 means authentication needs operator action.
- Gmail sync status 1 can represent a partial cache update; its JSON describes
  failed and unresolved messages and whether a retry is required.
- Gmail sync status 2 means authentication needs operator action.
- Account-tag or enrichment failure leaves the refresh incomplete even if the
  preceding cache update succeeded.

Report the confirmed account, live or cached mode, each exact exit status, the
Gmail sync's partial and retry fields when present, and the report path and
status returned by enrichment. Never label a partial or mixed result complete.

Exit 0 alone does not prove report regeneration. Enrichment can return
`status: "skipped"` with no path when no report can be built. Report that outcome
as skipped, not refreshed. Claim regeneration only for `written` after verifying
the returned path is the approved account report and its artifact is nonempty.

A credentialed local run is private operational evidence only. Sanitize retained
counts and statuses; do not retain message content, addresses, tokens, or local
paths. Live Gmail requires a configured and authorized Google identity. Record
what the current run actually returned; help output or a successful cached read
does not establish live access or a completed sync.

To refresh every configured account, first enumerate the accounts and obtain
explicit approval for that full set. Only then may the scoped flags be omitted;
an unscoped request is not implicit approval.
