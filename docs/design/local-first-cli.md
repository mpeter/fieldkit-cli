# Local-first CLI design

This guide helps contributors preserve fieldkit's core design while adding a
command, integration, or persisted feature.

## Preconditions

- The base package must work without an external integration installed.
- A command can write only to a configured workspace, runtime-data root, or
  documented fieldkit configuration location.
- A command that crosses a network or changes an external system must have a
  bounded timeout and an explicit user-facing failure mode.

## Invariants

- Commands adapt input and output; domain modules own behavior.
- Configuration, workspace content, and runtime data remain separate roots.
- Optional integrations are imported only when their command is invoked.
- Exit status distinguishes success, retryable failure, required user action,
  and invalid data.
- Public examples use fictional data and never require a credential.

## Design rationale

fieldkit is designed as a local-first command-line tool so that a useful first
workflow is inspectable and offline. Integrations add capability after explicit
installation and configuration; they do not turn the base package into a
network service.

The architecture chose thin command adapters over command-owned domain logic.
That keeps a behavior reusable by hooks and tests, and gives each convention
one authoritative implementation. Persistent roots are intentionally separate
so a checkout, user work, and generated data cannot be confused or committed
together.

## Architecture enforcement

Tach enforces module boundaries within the wheel's `src/fieldkit` package.
Its source roots are `src` and `tests`. These roots must not overlap because
Tach maps files against the first matching root.

Checkout-only `hooks/` and `scripts/` are outside that module graph. The hooks
still consume domain behavior rather than owning it.
`uv run python scripts/check_hook_boundaries.py` preserves their original
allowlist (`fieldkit`, `fieldkit.config`, `fieldkit.enrich`, `fieldkit.errors`),
resolves imports against the most specific Tach module, and rejects package
imports of checkout hooks. Local PR/full gates and hosted lint run this check.
Tach impact selection is a local speed aid only. Hosted `Test (pytest)` runs the
entire suite in parallel on every code PR, so hook and script tests, and any test
a selection would miss, always run before merge.
Full-suite JUnit evidence requires at least one executed test, counted as the
reported total minus skipped tests. Empty or entirely skipped results retain
their counts and an actionable diagnostic, but fail the evidence step. Runs
with passing execution and some skipped tests remain valid. A documentation
no-impact classification is a separate workflow decision and does not produce
a passing full-suite JUnit report.
The normalized `selection_reason` is `full-suite-policy`: hosted testing runs
the complete suite for every non-doc change, including dependency-only changes.

## Contact enrichment checkpoints

Contact enrichment persists version 1 checkpoints with an account filter, a
deterministic SHA-256 fingerprint of the ordered raw contacts, and the processed
count. The fingerprint includes all raw fields and ignores dictionary key order.
Only matching identities with an in-range count can resume. Legacy, unsupported,
or mismatched checkpoints restart with a warning; existing enriched records and
memory files are retained. Restarting appends results and can retain duplicates.
An empty input makes no checkpoint or enriched-output writes. Batch output is
written atomically before advancing the checkpoint; separate output/checkpoint
writes do not provide exactly-once processing after a crash.

## Contributor action

Before changing a boundary, read the relevant decision record in
[`docs/adr`](../adr/0001-local-first-roots.md) and update this guide or that
record when its preconditions, invariants, or trade-offs change.

## Read-only pursuit assessment

The pursuit I/O domain supplies a typed per-file read result for audit, health,
and forecast. It retains either frontmatter/body or a sanitized failure reason.
Report consumers partition scanned files into included rows, intentional
exclusions, and failed inputs. An unreadable record cannot become an absent deal
in a successful report. Partial reports exit 1 and carry failure paths relative
to the account root; source YAML and OS error details are never copied into
parse diagnostics. This contract adds forecast assessment metadata. Health
JSON keeps its array format in every case, because the web dashboard and
reconciliation scripts parse it as one; incompleteness shows on stderr and in
the exit code. Readers make no workspace or runtime writes.
