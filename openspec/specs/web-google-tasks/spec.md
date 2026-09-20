# web-google-tasks Specification

## Purpose
Define the current behavioral contract for web-google-tasks, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Dashboard reads the Google Tasks `fieldkit` list via `gws`

The web dashboard MUST expose a "Tasks" view that reads the Google Tasks list
named `fieldkit` by invoking the `gws` CLI (`gws tasks tasks list`), and MUST
NOT implement a separate Google Tasks API client, OAuth flow, or MCP-based
integration to obtain this data. The reader MUST follow every `nextPageToken`
from both task-list discovery and task enumeration; a dashboard view containing
only the first page of either collection is incomplete.

#### Scenario: Tasks tab renders the current list
- **GIVEN** `gws` is installed, keyring-authenticated, and the `fieldkit` task
  list contains at least one `needsAction` task tagged `section:today`
- **WHEN** the operator opens the dashboard's Tasks tab
- **THEN** the tab renders that task under a "Today" grouping, with its due
  date (if present) and its `<!-- gtask:id -->` anchor preserved internally
  for later write operations

#### Scenario: `gws` is unauthenticated or errors
- **GIVEN** `gws tasks tasks list` exits non-zero or requires reauthentication
- **WHEN** the operator opens the dashboard's Tasks tab
- **THEN** the tab renders an explicit error state naming the failure (not a
  generic 500) and instructs the operator to run the `gws` auth flow in a
  terminal; the rest of the dashboard's tabs MUST continue to function
  (per-source failure isolation, matching existing `WebDataError` behavior)

#### Scenario: Task-list discovery or task enumeration spans multiple API pages
- **GIVEN** a task-list or task response includes `nextPageToken`
- **WHEN** the dashboard loads the Tasks tab
- **THEN** it requests the next page with that token and renders tasks from
  the named `fieldkit` list across every page exactly once

### Requirement: Task parsing follows the `task-sync` anchor and section-tag contract

The dashboard's Google Tasks parsing MUST use the same `<!-- gtask:id -->`
anchor scheme and `section:today` / `section:active` notes-tag convention
defined by the `fieldkit-home` `task-sync` skill, so that a task read or
written through the dashboard remains consistent with the next `/task-sync`
reconcile pass.

#### Scenario: A task written via the dashboard round-trips through task-sync
- **GIVEN** the operator creates a task from the dashboard's Tasks tab with
  `section:active` and an account prefix
- **WHEN** `/task-sync` next runs its reconcile pass against the same
  `fieldkit` list
- **THEN** the task appears in `TASKS.md`'s managed region under the `active`
  sub-block with its anchor intact, with no duplicate line and no lost
  section/account tagging

The compatibility test MUST use the documented wire grammar as its independent
oracle: created-task notes are exactly `section:<today|active>` on line 1 and,
when present, `account:<slug>` on line 2; the expected managed-region line is
exactly `- [ ] <title> [— due <date>] <!-- gtask:<id> -->`, with the anchor last.

### Requirement: Write actions on Google Tasks route through the companion gate

The dashboard MUST route every action that creates or completes a task in the
Google Tasks `fieldkit` list through a previewable `fieldkit gtask` command
executed by `fieldkit companion run`,
subject to the same `companion.tier` configuration (`read` / `propose` /
`act`) that governs agent-initiated writes, in addition to the existing
dashboard bearer-token write gate. The dashboard MUST NOT invoke a Google
Tasks write command directly, bypassing the companion gate.

#### Scenario: Write control disabled at `read` tier
- **GIVEN** `companion.tier` is `read` (the fail-closed default)
- **WHEN** the operator views a task on the dashboard's Tasks tab
- **THEN** the "complete" and "create task" controls are disabled and labeled
  with the current tier and the minimum tier required, and no Google Tasks
  write command is ever invoked from that state

#### Scenario: Task writes are disabled without a configured web token
- **GIVEN** the dashboard is running in tokenless loopback mode
- **WHEN** a client posts to a task-write or proposal-approval route
- **THEN** the route returns HTTP 403 with the existing "serve with a token"
  remediation and neither writes a proposal nor invokes `companion run`

#### Scenario: Configured token is missing or wrong
- **GIVEN** the dashboard was started with a bearer token
- **WHEN** a client posts to a task-write or proposal-approval route without
  that exact token
- **THEN** the route returns HTTP 401 and performs no side effect

#### Scenario: Write proposed at `propose` tier
- **GIVEN** `companion.tier` is `propose`
- **WHEN** the operator clicks "complete" on a task in the dashboard
- **THEN** the dashboard writes a proposal file to
  `<fieldkit_data>/companion-outbox/` recording the intended `fieldkit gtask`
  argv in versioned structured metadata, and
  does not call Google Tasks directly; the proposal becomes visible on the
  Outbox tab for a subsequent approve action

#### Scenario: Write executed at `act` tier with an allowlisted command
- **GIVEN** `companion.tier` is `act` and `companion.act_allowlist` contains
  the command-scoped `gtask complete --confirm` entry
- **WHEN** the operator clicks "complete" on a task in the dashboard
- **THEN** `fieldkit companion run` gate-checks, executes the `gws` command,
  and journals the outcome in the same append-only companion journal used for
  agent-initiated actions

#### Scenario: Write denied at `act` tier with no matching allowlist entry
- **GIVEN** `companion.tier` is `act` and no allowlist entry matches the
  intended argv
- **WHEN** the operator clicks "complete" on a task in the dashboard
- **THEN** the action is denied (exit 3 from the underlying gate), the denial
  is journaled, and the dashboard surfaces the denial to the operator rather
  than silently failing or retrying

### Requirement: Feed and Outbox tabs render companion loop state

The dashboard MUST expose a "Feed" tab rendering `fieldkit companion feed
--json` output and an "Outbox" tab rendering pending files under
`<fieldkit_data>/companion-outbox/`, each following the dashboard's existing
`DataSource` isolation pattern so a failure in one tab's data source does not
affect the others.

Every newly created proposal MUST be one atomically published version-1 JSON
envelope owned by `fieldkit.companion.outbox`, named
`<date>-<item-id>-<slug>.proposal.json`, with `version`, `item_id`,
`created_at`, `command_argv` as a JSON string array or null, and `markdown` as
the human review surface. A legacy Markdown proposal MUST remain readable and
MUST be shown as non-approvable rather than having argv reconstructed from
prose. Replacement of a deterministic basename MUST atomically replace
Markdown and argv as one generation.

The outbox domain MUST be the single source for listing current envelopes and
legacy Markdown proposals. Companion suppression and the morning brief MUST
recognize current envelopes, so a pending proposal retires its attention item
and remains included in the brief's pending-proposal count and pointer.

#### Scenario: Outbox approve action executes the recorded proposal
- **GIVEN** a pending outbox proposal file exists with a recorded target argv
- **WHEN** the operator clicks "approve" on that proposal in the Outbox tab
- **THEN** the dashboard invokes `fieldkit companion run` with the argv
  recorded at proposal-write time (not re-parsed from the file's prose at
  click time), gated by the write-auth bearer token

#### Scenario: Approval is at-most-once and preserves a waiting replacement
- **GIVEN** approval has claimed a current envelope and a writer attempts to
  publish the same deterministic basename while the recorded action runs
- **WHEN** the action succeeds, fails, or succeeds followed by cleanup failure
- **THEN** the claimed generation executes at most once, cleanup cannot remove
  or overwrite the waiting generation, and any retained execution marker is
  non-approvable

### Requirement: No GAS involvement in interactive task management

This capability MUST NOT introduce any write path, form, or editing UI in the
`fieldkit-home` GAS project (`gas/`). The interactive Google Tasks surface
MUST live entirely in the local `fieldkit web` dashboard.

#### Scenario: Feature review confirms no GAS surface changed
- **GIVEN** this change is implemented
- **WHEN** a reviewer inspects the diff
- **THEN** no files under `fieldkit-home/gas/` are modified, and no new
  inbound HTTP path is added to the GAS `doPost`/`doGet` handlers for task
  data

### Requirement: Outbox approve action executes the recorded proposal

An authenticated approval MUST claim the selected version-1 proposal, execute
only its recorded validated argv through the companion gate, and record an
approved lifecycle outcome only after successful execution and claim completion.

#### Scenario: Successful approval records its terminal outcome
- **GIVEN** an authenticated operator selects an approvable proposal
- **WHEN** its recorded action succeeds and the claim is completed
- **THEN** the pending proposal is removed
- **AND** an approved lifecycle outcome identifies the proposal and item

#### Scenario: Failed approval remains pending
- **GIVEN** an authenticated operator selects an approvable proposal
- **WHEN** its recorded action fails
- **THEN** the proposal is restored for review
- **AND** no approved lifecycle outcome is recorded
