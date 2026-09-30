# watch-all-domain-dispatch Specification

## Purpose
Define the current behavioral contract for watch-all-domain-dispatch, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: aggregate dispatch uses domain calls
The aggregate watcher command MUST call the four local watcher domain functions
in `_WATCHER_ORDER`, plus Backstory health and draft queue when their endpoints
are configured and Slack threads when `--slack` is selected. It MUST preserve
effective defaults, dry-run, force, brief output, status persistence, and
ordinary-exception continuation for the selected set.

#### Scenario: all watchers run in order
- **WHEN** the operator requests all watchers with both optional endpoints configured and Slack selected
- **THEN** the seven domain calls occur in `_WATCHER_ORDER` with the documented arguments

### Requirement: output capture is scoped
The aggregate command MUST redirect both stdout and stderr around each domain
call only, restore them before aggregate errors/summary/auth handling, and leave
file logging unchanged.

#### Scenario: domain prints and fails normally
- **WHEN** a domain prints a marker and raises an ordinary exception
- **THEN** captured markers are discarded, the existing exception is logged after stream restoration, code 1 is recorded, and later watchers run

#### Scenario: domain raises an auth error
- **WHEN** a domain prints and raises an `AuthError` subclass
- **THEN** streams are restored and the auth error reaches the exit-2 boundary

### Requirement: Typed execution result admission
The aggregate command MUST admit only an exact canonical `WatcherRunResult`
after explicit projection of specialized results. Bare integers, booleans,
strings, missing results, foreign classes, and ordinary exceptions MUST become
incomplete fatal results with exit 1. Completed live success or partial work
MUST carry this invocation's exact `written` status response. Missing, failed,
or skipped required persistence MUST remain nonpassing.

#### Scenario: domain returns an unproven result
- **WHEN** a domain returns an integer, `False`, `None`, a string, or a foreign result
- **THEN** aggregate status records a fatal outcome and exits 1 even with `--allow-partial`

### Requirement: daily guard prevents dispatch
The aggregate command MUST keep every domain call behind the existing daily
guard unless force is requested. Tests MUST observe all seven domain functions.

#### Scenario: the daily guard suppresses a run
- **WHEN** one daily-status snapshot suppresses a non-forced aggregate run
- **THEN** every domain mock remains uncalled

#### Scenario: Previous run is nonpassing
- **WHEN** a same-day record has a partial, fatal, missing, or unrecognized outcome
- **THEN** dispatch and publication are suppressed and the command exits 1 even with `--allow-partial`
- **AND** the previous status is not replaced

### Requirement: Partial allowance requires current execution evidence
The aggregate MUST allow a nonzero result only on a live completed partial pass
whose every nonzero step is completed partial with exit 1 and exactly written
status. Its own status write MUST return exactly `written`. It MUST persist
`partial`, not `ok`, when allowing that pass. Fatal results, incomplete work,
preview failures, and codes 2 or 3 MUST NOT be allowed. Returned-code priority
MUST remain 3, then 2, then 1; authentication and configuration exceptions MUST
propagate through their existing boundary.

#### Scenario: Completed partial pass is allowed
- **WHEN** all selected steps have valid current execution evidence and only completed partial failures remain
- **AND** aggregate partial status is successfully written during this invocation
- **THEN** `--allow-partial` exits 0 while the recorded outcome remains partial

#### Scenario: Leaf status fails while aggregate status succeeds
- **WHEN** a leaf's required status write fails and aggregate status publication succeeds
- **THEN** aggregate status is fatal and the command remains nonzero with `--allow-partial`

### Requirement: Empty report rejection retains data-error identity
Typed empty-output failures MUST produce fatal incomplete aggregate results
with exit 3 and fixed diagnostics that reveal neither input text nor private
paths. Ordinary exceptions with similar messages MUST NOT receive that typed
classification.

#### Scenario: Brief renders whitespace
- **WHEN** newly rendered brief content contains no non-whitespace text
- **THEN** the previous report remains unchanged, no publication is claimed, and aggregate exits 3

### Requirement: countdown defaults have one source
Countdown thresholds MUST remain defined in the domain module and agree with
the leaf Click defaults; aggregate contract-expiry calls MUST omit threshold
kwargs and use domain defaults.

#### Scenario: defaults are inspected
- **WHEN** tests inspect the domain, leaf command, and aggregate call
- **THEN** countdown defaults agree and contract-expiry receives no threshold overrides
