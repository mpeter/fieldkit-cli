---
last_reviewed: 2026-09-13
covers:
  - src/fieldkit/cli_exit.py
  - src/fieldkit/__main__.py
audience: both
---

# Exit Codes Reference

fieldkit command outcomes use four canonical codes. These codes let agents and
orchestrators classify outcomes without parsing stdout or stderr. A process
interrupted with Ctrl+C exits `130` instead of using the application taxonomy.

Exit codes are guaranteed by a two-layer boundary in `src/fieldkit/cli_exit.py` and
`src/fieldkit/__main__.py`. Library code raises typed exceptions; `handle_cli_exception()`
maps them to the canonical code. The guarantee holds for every command group:

- **Per-command layer:** `cli_main()` context manager catches exceptions inside a command
  block and calls `sys.exit()` with the canonical code.
- **Dispatcher backstop:** `__main__.main()` catches any exception that escapes a leaf
  command not wrapped in `cli_main()`, routes it through `handle_cli_exception()`, and
  returns the canonical code. No domain exception produces a raw Python traceback.

**Precision note:** the two layers guarantee both the canonical range and the
exception-to-code semantics for command failures. Domain and command code raise
typed exceptions; the dispatcher is the final process boundary.

---

## Code Table

| Code | Name | Meaning | What to do |
|------|------|---------|------------|
| `0` | **Success** | Operation completed, or there was nothing to do | Continue |
| `1` | **Partial or policy failure** | Some records failed, or an explicit strict policy found attention items | Check stderr/output; retry partial work or review strict findings |
| `2` | **Auth failure** | A credential is expired or missing | Re-authenticate before retrying (see relevant auth guide) |
| `3` | **Data error** | Validation or data error | Investigate; retry will not help |

---

## Exception-to-Code Mapping

`cli_main()` catches the following exception types and maps them automatically:

| Exception | Condition | Exit code |
|-----------|-----------|-----------|
| `ConfigError` | Missing or invalid `config.yaml` | `3` |
| `AuthError` (and subclasses: `SFAuthError`, `GmailAuthError`, `ShadowbotAuthError`) | Credential missing or expired | `2` |
| `FrontmatterStalenessError` | File modified since last read | `1` |
| `GmailSyncPartialError` | Gmail sync omitted one or more requested messages | `1` |
| `PursuitStaleError` | Pursuit stale relative to the live SF record | `1` |
| `LLMError(category="auth")` | Vertex AI credential failure | `2` |
| `LLMError(category="rate-limit")` | Provider rate limit hit | `1` |
| `LLMError(category="general")` | Any other LLM failure | `3` |
| `click.exceptions.Exit` | Used by `--help`, `--version` | code from exc (`Exit(None)` → `1`) |
| `click.ClickException` (including usage errors) | Invalid invocation or missing target; concise stderr | `3` |
| Companion malformed watcher status | Repair watcher state; concise stderr | `3` |
| Any other `Exception` | Unhandled error | `3` |

`KeyboardInterrupt` is not caught by either application boundary. The shell
reports an interrupted command as exit `130`.

`fieldkit pursuit health --strict` and `fieldkit pursuit projects --strict` use exit `1` as an explicit policy result when their reports contain attention findings. Without `--strict`, a complete valid report exits `0` regardless of its findings.

---

## Common commands that exit 1

These commands exit `1` when they produce a partial result or report findings:

- **`fieldkit pursuit audit`**: one or more pursuit files contain schema errors,
  warnings, or malformed frontmatter. Fix the reported files, then run the audit
  again.
- **`fieldkit sf drift`**: the drift report is incomplete because a pursuit file
  could not be read, an `sf_opportunity_id` is not a Salesforce record ID, a
  linked pursuit has no recognized `stage`, or a Salesforce request failed.
  Drift, including an opportunity Salesforce no longer has, is reported with exit
  `0`.

---

## Common Commands That Exit 2

These commands exit `2` when their required credential is absent or expired:

- **`fieldkit sf session-check`** — Salesforce session cookie (`sf-cookies.json`) is
  missing or the `sid` has expired. Run `fieldkit auth sf` to refresh.
- **`fieldkit sf drift`** — the Salesforce session is missing or expired and at
  least one pursuit needs fetching. The run stops instead of reporting partial
  results. Run `fieldkit auth sf` to refresh.
- **`fieldkit auth shadowbot`** — ShadowBot Chrome cookie is missing or the session
  has expired. Run `fieldkit auth shadowbot` to re-authenticate.
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
- **`fieldkit pursuit create`** and **`fieldkit pursuit rename`**: the name would
  produce a reserved pursuit file name (`template` or `gmail-intel`), which
  reports skip. Nothing is written; choose a different name.
- **`fieldkit pursuit rename`**: `--to` is not already a pursuit slug (lowercase
  letters, digits and single hyphens, as `pursuit create` produces), or
  `--account` or `--from` contains a path separator or is empty, `.` or `..`.
  Nothing is written; the message suggests the slug form of `--to`.
- **`fieldkit sf schema`**: the Salesforce describe response contains no eligible
  field. No record is requested; check the object name and its field permissions.
- **`fieldkit sf drift`**: the workspace has no `accounts` directory, `--account`
  is not the name of an existing directory directly under `accounts/` (a path
  such as `..` or `a/b` is rejected), or, when at least one pursuit needs
  fetching, no `sf_org_url` is configured. Run
  `fieldkit init` or correct the configuration, then run it again.
- Any command that reads `config.yaml` when the file is absent, malformed, or missing a
  required setting — fix the local configuration or run `fieldkit init`, then re-run.
- Any command that encounters an unhandled exception — the full traceback is printed to
  stderr to aid investigation.

---

## Reading Error Messages

All error output goes to **stderr**, never stdout. This keeps stdout clean for
machine-readable output (JSON, YAML, pipe targets) while errors remain visible in
the terminal.

Error lines from `cli_main()` follow this format:

```
[cli_exit] <category> — <action required>: <detail>
```

Examples:

```
[cli_exit] Config error — investigation required: Config file not found: ~/.config/fieldkit/config.yaml
[cli_exit] Auth failure — user action required: [LLMError/auth] Authentication failed for model 'vertex_ai/claude-sonnet-4-6': ...
[cli_exit] Rate limit — partial failure: [LLMError/rate-limit] Rate limit exceeded ...
[cli_exit] Unhandled exception — investigation required:
Traceback (most recent call last):
  ...
```

For exit `2` errors, the message includes the relevant authentication action.
Configuration errors on exit `3` use a concise diagnostic and repair hint;
unhandled exit-3 failures include a traceback for investigation.

---

## Click usage errors

In the 1.0 contract, a Click `UsageError` exits `3` rather than Click's default
`2`:

A malformed invocation (unknown command, bad flag, wrong number of arguments) previously
exited `2` (EXIT_AUTH). It now exits `3` (EXIT_DATA) — retrying without fixing the
command will not help. Update any orchestration that checked `rc == 2` to detect bad
invocations; those should now check `rc == 3`.

Note: `CliRunner`-based tests and code that uses Click's `standalone_mode=True` are
unaffected — only code that calls `fieldkit.__main__.main()` directly sees the new code.
