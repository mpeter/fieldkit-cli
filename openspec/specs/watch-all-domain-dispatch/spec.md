# watch-all-domain-dispatch Specification

## Purpose
Define the current behavioral contract for watch-all-domain-dispatch, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: aggregate dispatch uses domain calls
The aggregate watcher command MUST call exactly the seven documented domain
functions in `_WATCHER_ORDER`, preserving effective defaults, dry-run, force,
brief output, status persistence, and ordinary-exception continuation.

#### Scenario: all watchers run in order
- **WHEN** the operator requests all watchers
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

### Requirement: result codes are strict integers
The aggregate command MUST accept a result only when `type(result) is int`; it
MUST normalize `False`, `None`, and strings to code 1.

#### Scenario: domain returns a non-integer result
- **WHEN** a domain returns `False`, `None`, or a string
- **THEN** aggregate status records code 1

### Requirement: daily guard prevents dispatch
The aggregate command MUST keep every domain call behind the existing daily
guard unless force is requested. Tests MUST observe all seven domain functions.

#### Scenario: the daily guard suppresses a run
- **WHEN** `was_run_today` suppresses a non-forced aggregate run
- **THEN** every domain mock remains uncalled

### Requirement: countdown defaults have one source
Countdown thresholds MUST remain defined in the domain module and agree with
the leaf Click defaults; aggregate contract-expiry calls MUST omit threshold
kwargs and use domain defaults.

#### Scenario: defaults are inspected
- **WHEN** tests inspect the domain, leaf command, and aggregate call
- **THEN** countdown defaults agree and contract-expiry receives no threshold overrides
