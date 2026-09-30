---
last_reviewed: 2026-09-27
covers:
  - src/fieldkit/cli_exit.py
  - src/fieldkit/__main__.py
  - src/fieldkit/issue/github.py
  - src/fieldkit/issue/milestone.py
  - src/fieldkit/commands/ingest/run.py
  - src/fieldkit/commands/ingest/promote.py
  - src/fieldkit/commands/ingest/reprocess.py
audience: both
---

# Exit Codes Reference

fieldkit command outcomes use four canonical codes. These codes let agents and
orchestrators classify outcomes without parsing stdout or stderr. A process
interruption that reaches the dispatcher exits `130` instead of using the
application taxonomy. Credential-recovery prompts handle cancellation as an
authentication outcome, as described below.

Exit codes are normalized by a two-layer boundary in `src/fieldkit/cli_exit.py`
and `src/fieldkit/__main__.py`. Library code raises typed exceptions;
`handle_cli_exception()` maps them to the canonical code. Unsuppressed explicit command exits
preserve exact built-in integer statuses `0` through `3`; `SystemExit(None)` means success,
while `click.exceptions.Exit(None)` and other invalid payloads (including
booleans and integer subclasses) become `3`. Exact built-in integer callback returns use the same status
validation: `0` through `3` are preserved, and other integers become `3` with a
fixed diagnostic that does not include the value. Noninteger callback returns,
including `None`, booleans, and integer subclasses, retain normal Click success behavior. Here,
“noninteger callback return” means any result whose type is not exactly built-in `int`.
An explicit Click exit suppressed by a context resource retains normal Click success behavior,
including when its payload is invalid. The guarantee holds for every command group:

- **Per-command layer:** `cli_main()` context manager catches exceptions inside a command
  block and calls `sys.exit()` with the canonical code.
- **Dispatcher backstop:** `__main__.main()` catches any exception that escapes a leaf
  command not wrapped in `cli_main()`, routes it through `handle_cli_exception()`, and
  returns the canonical code. Unhandled failures retain a traceback for investigation.

**Precision note:** the two layers guarantee both the canonical range and the
exception-to-code semantics for command failures. Domain and command code raise
typed exceptions; the dispatcher is the final process boundary.

---

## Code Table

| Code | Name | Meaning | What to do |
|------|------|---------|------------|
| `0` | **Success** | Operation completed, or there was nothing to do | Continue |
| `1` | **Partial, retryable, or policy failure** | A request or some records failed, or an explicit strict policy found attention items | Check stderr/output; retry transient work or review strict findings |
| `2` | **Auth failure** | A credential is expired or missing | Re-authenticate before retrying (see relevant auth guide) |
| `3` | **Data error** | Validation or data error | Investigate; retry will not help |

---

## Exception-to-Code Mapping

The handler owns domain failures. The dispatcher owns Click usage, explicit
command exits, callback return statuses, and process interruption. Rows marked `handler` are routed by
`handle_cli_exception()` from either layer; rows marked `dispatcher` are handled
directly by `fieldkit.__main__.main()`.

| Exception | Boundary | Condition | Exit code |
|-----------|----------|-----------|-----------|
| `ConfigError` | handler | Missing or invalid configuration or required command setup | `3` |
| `EmptyOutputError` | handler | A report is empty or whitespace-only, or its zero-byte cleanup fails; fixed recovery guidance | `3` |
| `MissingOptionalDependencyError` | handler | Selected command needs an optional installation profile | `3` |
| `SQLiteSnapshotError(reason="active")` | handler | Database has active writers; retry after they finish | `1` |
| `SQLiteSnapshotError(reason="journal" or "unverified")` | handler | Snapshot safety could not be established | `3` |
| `GitHubRequestError` | handler | Transient request, timeout, or explicit rate-limit rejection | `1` |
| `GitHubDataError` | handler | Invalid managed issue or provider response data | `3` |
| `GitHubCreationUncertainError` | handler | An irreversible create may have landed without a proven identity | `3` |
| `FrontmatterStalenessError` | handler | File modified since last read | `1` |
| `GmailSyncPartialError` | handler | Gmail sync omitted one or more requested messages | `1` |
| `GmailSyncRestartRequiredError` | handler | Gmail refresh must restart from a fresh bounded scan | `1` |
| `GoogleCredentialRefreshRetryableError` | handler | Google credential refresh failed transiently | `1` |
| `SalesforceSyncPartialError` | handler | Transient local pursuit scan/read failure or retryable sync omissions | `1` |
| `RoutingReadRetryableError` | handler | Ingest pursuit scan or read failed transiently; retry after local access is restored | `1` |
| `SFAuthError` | handler | Salesforce authentication failed; refresh with `fieldkit auth sf` | `2` |
| `SFConditionalWriteConflict` | handler | Guarded Salesforce write is stale; reread before another write | `3` |
| `SFConditionalWriteOutcomeUnknown` | handler | Salesforce may have written the record; reread before another write | `3` |
| `SFNotFoundError` | handler | Salesforce record not found; check the record reference | `3` |
| `SFDataAccessError` | handler | Salesforce access denied; check record permissions | `3` |
| `SFAPIError` | handler | Salesforce request failed; check availability and configuration | `3` |
| `AuthError and subclasses` | handler | Other credential missing or expired | `2` |
| `PursuitStaleError` | handler | Pursuit stale relative to the live SF record | `1` |
| `LLMError(category="auth")` | handler | Vertex AI credential failure | `2` |
| `LLMError(category="rate-limit")` | handler | Provider rate limit hit | `1` |
| `LLMError(category="general")` | handler | Any other LLM failure | `3` |
| exact `FieldkitError` | handler | Deliberate bounded abort with a clean diagnostic | `3` |
| Any other `Exception` | handler | Unhandled error with a traceback | `3` |
| `click.ClickException` | dispatcher | Usage or other user-facing Click failure | `3` |
| `click.exceptions.Exit` or `SystemExit` | dispatcher | Unsuppressed exit with exact built-in integer status `0` through `3` is preserved | `0` through `3` |
| `SystemExit(None)` | dispatcher | Python explicit exit without a status means success | `0` |
| Invalid explicit exit payload | dispatcher | Unsuppressed exit with `click.exceptions.Exit(None)`, booleans, integer subclasses, nonintegers, or integers outside `0` through `3` | `3` |
| Exact built-in integer callback return | dispatcher | Preserve `0` through `3`; any other exact built-in integer becomes data error | `0` through `3` |
| Noninteger callback return | dispatcher | Normal Click callback result, including `None` and booleans | `0` |

Known Salesforce exceptions use fixed diagnostics without copying transport,
response, or credential details. This table describes exceptions that reach the
canonical handler. Command adapters that handle an `SFAPIError` locally can
retain their documented exit `1`; that is not the uncaught exception's mapping.

`KeyboardInterrupt` passes through the per-command layer. The dispatcher handles
it and Click's interruption-caused `Abort` wrapper as exit `130`, without a
traceback. EOF and explicit Click cancellation that reach the dispatcher return
`1`, not a signal status. Cancelling the interactive `fieldkit auth sf` or
`fieldkit auth shadowbot` credential prompt (Ctrl+C or EOF) returns `2`: the
required authentication is still incomplete. No new credentials are written by
the cancelled prompt. Run the authentication command again when ready to supply
the credential.
User-facing Click exceptions that reach the dispatcher, including usage errors,
return `3` with their error message instead of an unhandled-error traceback.

`fieldkit pursuit health --strict` and `fieldkit pursuit projects --strict` use exit `1` as an explicit policy result when their reports contain attention findings. Without `--strict`, a complete valid report exits `0` regardless of its findings.

---

## Common commands that exit 1

These commands exit `1` when they produce a partial result or report findings:

- **`fieldkit issue list`, `show`, and `board`**: GitHub times out, is
  temporarily unavailable, or explicitly reports a rate limit. No empty result
  or “not found” claim is emitted for a failed read. Authentication or
  repository permission failures exit `2`; malformed provider data and a
  missing GitHub CLI exit `3`. With `--json`, read-error paths leave stdout
  empty and report safe recovery guidance on stderr. Parse the result only
  after a successful exit.
- **`fieldkit issue sync-milestone`**: one or more candidate transitions fail
  transiently and no authentication or invalid-data failure has higher
  priority. The command attempts the remaining candidates and emits a bounded
  outcome document even on a non-zero batch exit so callers can reconcile
  already-applied transitions.

- **`fieldkit pursuit audit`**: one or more pursuit files contain schema errors,
  warnings, or malformed frontmatter. Fix the reported files, then run the audit
  again.
- **`fieldkit ingest run --pipeline transcript-ingest`**: another run holds the
  pipeline lock, or one or more sources could not complete. A prepared-output
  replay failure retains its saved intent for retry; already completed sources
  remain completed. Inspect the diagnostic and repair the output conflict or
  wait for the other run before retrying.
- **`fieldkit ingest reprocess --pipeline transcript-ingest`**: an artifact
  fails, recovery remains pending or is excluded by the selection, another run
  holds the pipeline lock, or saved recovery targets an older pipeline version.
  A dry-run with retained recovery also exits `1`, as does omitting both
  `--from-version` and `--force` in either a live run or a dry-run. An interrupted
  artifact loop exits `1`. Repair the reported conflict and retry the intended
  selection; completed replacements remain completed.
- **`fieldkit ingest promote`**: `TASKS.md` is missing, neither a meeting file
  nor `--recent N` was supplied, or another writer holds the task-file lock past
  the bounded wait. Create the task file, select the meeting input, or wait for
  the other writer as directed by the diagnostic. Earlier promoted items remain
  saved.

---

## Common Commands That Exit 2

These commands exit `2` when their required credential is absent or expired:

- **`fieldkit sf session-check`** — Salesforce session cookie (`sf-cookies.json`) is
  missing or the `sid` has expired. Run `fieldkit auth sf` to refresh.
- **`fieldkit auth shadowbot`** — ShadowBot Chrome cookie is missing or the session
  has expired. Run `fieldkit auth shadowbot` to re-authenticate.
- **`fieldkit ingest run` and `fieldkit ingest reprocess`** — the Google token
  file is missing, or a Docs request returns a terminal HTTP `401`, when fresh
  transcript processing needs the service. Run
  `fieldkit auth google`, then retry. Replaying already saved output does not
  require Google credentials.
- **`fieldkit gmail sync`** — Interactive Google OAuth consent is required but stdin
  is not a TTY, such as in a systemd service or cron job. Run `fieldkit gmail sync`
  in an interactive terminal, open the printed authorization URL, and then retry
  the automated run.
---

## Common Commands That Exit 3

These commands exit `3` when data is invalid and a retry without a fix will not help:

- **`fieldkit pursuit audit`**: the configured root or account directory is
  missing, or the selected scope contains no pursuit files. Initialize or correct
  the workspace, then run the audit again.
- **`fieldkit ingest promote`**: meeting metadata or task provenance is invalid
  or ambiguous. Correct the reported input before retrying; earlier promoted
  items remain saved.
- **`fieldkit issue`**: a managed record or provider response is malformed, a
  requested resource disappeared during a milestone batch, or an irreversible
  create may have landed without returning a schema-valid positive GitHub issue
  number. Creation uncertainty is never retried automatically; reconcile the
  repository's issues before deciding whether to create again. Provider output
  is not copied into the diagnostic or batch result.
- Commands that require configuration when `config.yaml` is absent, malformed,
  or missing a required setting — fix the local configuration or run
  `fieldkit init`, then re-run. Optional configuration reads can use defaults
  when the file is absent, and `fieldkit init --minimal` can initialize a new
  workspace without an existing configuration file.
- Any command that encounters an unhandled exception — the full traceback is printed to
  stderr to aid investigation.

---

## Reading Error Messages

Human-readable diagnostics go to **stderr** so ordinary stdout remains available
for JSON, YAML, and pipe targets. When a caller explicitly requests a structured
result, fieldkit may emit a bounded outcome document on stdout even when the
process exits nonzero. Dispatcher-level `--json` usage errors likewise emit a
fixed invalid-result object on stdout. Always check the exit status before using
structured output as a successful result.

Many error lines from `cli_main()` follow this format:

```
[cli_exit] <category> — <action required>: <detail>
```

Examples:

```
[cli_exit] Config error — investigation required: Config file not found: ~/.config/fieldkit/config.yaml
[cli_exit] Model authentication failed — refresh provider credentials and retry.
[cli_exit] Model rate limit — retry later.
[cli_exit] Model request failed — check model configuration and provider availability.
[cli_exit] Unhandled exception — investigation required:
Traceback (most recent call last):
  ...
```

For exit `2` errors, the message includes the relevant authentication action.
Configuration errors on exit `3` use a concise diagnostic and repair hint;
unhandled exit-3 failures include a traceback for investigation.
Model-provider failures are handled separately: authentication exits `2`, rate
limits exit `1`, and other model failures exit `3`, with fixed guidance rather
than provider payloads or exception chains. Transcript ingest and reprocessing
stop before saving replacement intent when cleaning or extraction raises one of
these provider failures; they do not publish fallback output for that failure.

---

## Click usage errors

The installed `fieldkit` command routes malformed invocations (unknown commands,
bad flags, or wrong numbers of arguments) through `fieldkit.__main__.main()` and
exits `3`. Correct the invocation before retrying; reserve exit `2` handling for
authentication or user-action failures.

Tests or Python callers that invoke a Click command directly can bypass this
dispatcher and retain Click's default usage-error code `2`. Use the public
dispatcher when verifying fieldkit's process-level exit contract.
