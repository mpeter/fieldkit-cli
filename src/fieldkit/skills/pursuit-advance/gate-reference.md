# Pursuit Advance — Gate Reference

Use the [pursuit-advance workflow](SKILL.md) for identity, approval, and write
verification. The CLI's current preview is the policy authority; this reference
does not supply a second gate implementation or authorize manual frontmatter edits.

## Interpret the actual JSON result

The preview includes `from_stage`, `to_stage`, `gate_passed`, `gate_status`,
`reasons`, `override`, `advanced`, `dry_run`, and `note`. A dry run has
`advanced: false` even when its gate passes; it is not evidence of a saved stage.

`gate_status: pending` requires reading `reasons`. It can mean an unavailable
native qualification policy or a backward transition. Historical local scores
cannot clear either. An explicitly supplied nonempty override can make the
decision `override`; it does not establish native qualification readiness.
Qualification-independent transitions can pass without proving customer evidence.

Already closed pursuits cannot advance. Unknown stages, missing files, and
malformed source data require correction before retrying. Inspect the nonzero
exit and diagnostic instead of interpreting missing JSON as an empty pass.
Selectors that escape or redirect outside the configured pursuit namespace are
invalid data and are rejected before the source file is read.

## Verify an approved write

The canonical CLI records `stage`, `gate-status`, today's `last-transition`,
and an appended `transition-history` entry. The history includes `date`, `from`,
`to`, and `gate-result`; `override-reason` is included only for an override.
Do not manufacture an empty override field or backdate the capture entry.

Require the command's successful exit and `advanced: true`, then reread the
actual file. Report uncertain or failed writes as non-passing. A local stage
transition neither updates Salesforce nor proves the contract was executed.
