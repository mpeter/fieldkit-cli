## Purpose

Make the driver's degraded OpenCode execution state diagnosable when its per-run
log files cannot be created.

## Requirements

### Requirement: Log-capture fallback reports disabled rate-limit fast-abort

When the driver cannot create its per-run OpenCode log files, it MUST emit a warning
that states child output will not be captured and the rate-limit fast-abort guard is
disabled, so the process MAY wait for its normal one-hour timeout ceiling.

#### Scenario: Per-run log directory cannot be created
- **GIVEN** creating the per-run OpenCode log directory raises `OSError`
- **WHEN** the driver opens its output logs
- **THEN** it returns no log paths and emits a warning naming both unavailable
  output capture and disabled rate-limit fast-abort
