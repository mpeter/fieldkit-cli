# AGENTS.md — Watcher Infrastructure (`watch/`)

Shared runtime infrastructure for all fieldkit watchers: outcome model, run-status persistence, dedup, and path helpers.

## Outcome Model

Three outcomes — only `fatal` is a pipeline failure:

- `ok` — all records processed, zero failures. Watcher ran cleanly.
- `partial` — some records processed, some failures. Output was still produced. **`partial` is not a failure** — it means the watcher ran but some items had errors (e.g., one account's API call failed). Retry may help.
- `fatal` — zero records processed. The pipeline did not run meaningfully (e.g., config missing, auth failure before any work started).

Outcomes are written to `<fieldkit_home>/watchers/watcher-run-status.json` atomically (tmp rename). Agents and orchestrators MUST classify by outcome, not by stdout parsing.

## State File Contract

- Each watcher writes a JSON state file to track what it has already alerted on.
- Dedup is done by `dedup.alert_block_exists()`: scans the alerts markdown file for a `## <heading_prefix>` line. First run always produces alerts (no prior state).
- **GOTCHA:** The `detected_transition_date` sentinel in `pursuit-stalls` is written on first detection and **never updated**. It records when the stall was first noticed, not the current date. Do not overwrite it on subsequent runs — that would destroy the stall age signal.
- State files are in `<fieldkit_home>/watchers/`. Do not read or write them directly — use the watcher's own state helpers.

## Account Skip Signal

**GOTCHA:** `internal: true` in `accounts.yaml` causes the account to be skipped by the `backstory-health` watcher. This is intentional for internal accounts that have no external Backstory signal. Check for this flag before adding new accounts to watcher scope.

## Environment Inheritance (systemd)

Verified on this machine (Fedora 43, systemd user services):

- **Inherited automatically** via `systemctl --user set-environment`:
  - `GOOGLE_APPLICATION_CREDENTIALS` ✅
  - `GOOGLE_CLOUD_PROJECT` ✅
  - `ANTHROPIC_VERTEX_PROJECT_ID` ✅
  - `gcloud` binary in PATH ✅
  - `mcpjungle` at `http://localhost:8080` reachable ✅

- **NOT inherited** — must be in `EnvironmentFile=` for any watcher that needs them:
  - `BRAVE_API_KEY`
  - `TAVILY_API_KEY`
  - `SLACK_MCP_*`
  - `WORKSPACE_MCP_CREDENTIALS_DIR`

Recommended pattern: `EnvironmentFile=/home/<user>/work/fieldkit/.env` in the systemd service unit.

## Test Patching Gotcha (L01)

**GOTCHA:** When patching config path helpers in watcher tests, patch the **module-level binding** where the function is imported, not where it is defined.

```python
# WRONG — patches the definition site, not the call site
with patch("fieldkit.config._loader.get_fieldkit_data", ...):

# CORRECT — patches the binding in the watcher module
with patch("fieldkit.watch.pursuit_stalls.get_fieldkit_data", ...):
with patch("fieldkit.watch.morning_brief.get_watchers_dir", ...):
```

This applies to `get_fieldkit_data`, `get_fieldkit_home`, and any other config helper imported at module top level. The `@cache` decorator on some helpers means the first call wins — patch before any call is made.

## Path Helpers

All 8 watcher leaf modules import `get_watchers_dir()` from `fieldkit.config` (implementation change). It is `@_config_cache`-decorated and already cleared by `clear_config_caches()`. Tests patch the import-site binding `fieldkit.watch.<mod>.get_watchers_dir`.

## Timezone-Aware Date (historic regression)

**CONSTRAINT:** Never use `date.today()` in watcher code. Use `datetime.now(tz=UTC).date()` instead. `date.today()` is timezone-naive and returns different dates depending on the server's local clock — on UTC systems run overnight it disagrees with wall-clock date for the user's timezone.

All watcher files were migrated in Spec 023 (historic regression). If you see `date.today()` anywhere in `src/fieldkit/`, it is a regression to fix.
