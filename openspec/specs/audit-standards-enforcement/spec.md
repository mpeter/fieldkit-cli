# audit-standards-enforcement Specification

## Purpose
Define the current behavioral contract for audit-standards-enforcement, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Maintained audit inventory

The repository MUST provide a local `make audit-check` target that evaluates the eight named rules A01 through A08 and exits nonzero when any rule reports a violation.

#### Scenario: Clean repository

- **WHEN** `make audit-check` runs against a conforming checkout
- **THEN** A01 through A08 each report pass and the command exits zero

#### Scenario: Rule violation

- **WHEN** any maintained rule detects one or more violations
- **THEN** the output identifies the rule and every detected location and the command exits nonzero

### Requirement: Behavioral checker coverage

Each maintained audit rule MUST have isolated tests proving both detection of a representative violation and acceptance of a representative conforming fixture.

#### Scenario: Checker refactor

- **WHEN** an audit rule's implementation or source anchor changes
- **THEN** its fixture tests continue to prove the intended pass and fail behavior without depending only on the live repository tree

### Requirement: Shared Salesforce retry policy validation

A05 MUST validate gateway retry statuses against `fieldkit.config.retry.RETRY_TRANSIENT_STATUSES`, the repository's single retry-policy source, without importing project code.

#### Scenario: Required gateway statuses present

- **WHEN** the source declaration contains 500, 502, and 504
- **THEN** A05 passes

#### Scenario: Required gateway status absent

- **WHEN** any of 500, 502, or 504 is absent or the source declaration is missing
- **THEN** A05 reports the missing declaration or statuses and fails

### Requirement: No grandfathered A01 or A02 violations

Before CI enforcement lands, domain exception classes MUST satisfy A01 and unnecessary postponed-annotation imports under `src/fieldkit`, `tests`, and `hooks` MUST satisfy A02 without adding exemptions for current violations.

#### Scenario: New domain error

- **WHEN** a domain module defines an error inheriting directly from `Exception`
- **THEN** A01 fails with its source location

#### Scenario: Postponed annotation import

- **WHEN** a scanned Python file contains an unnecessary `from __future__ import annotations`
- **THEN** A02 fails with its source location

### Requirement: Pull-request enforcement

The existing `fast-checks` CI job MUST run `make audit-check` for code pull requests after dependency installation, using the same code-change condition as its other code checks.

#### Scenario: Code pull request

- **WHEN** the changes job reports `code=true`
- **THEN** the fast-checks job runs `make audit-check` and a violation fails the job

#### Scenario: Documentation-only pull request

- **WHEN** the changes job reports `code=false`
- **THEN** the audit step is skipped while the existing required context still reports
