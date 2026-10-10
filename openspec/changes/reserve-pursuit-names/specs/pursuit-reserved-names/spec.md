## ADDED Requirements

### Requirement: Reserved pursuit file names have one definition

The pursuit domain MUST define the reserved pursuit file names (`template.md`
and `gmail-intel.md`) in exactly one place and MUST expose a predicate that
matches a path's exact file name. Every command that enumerates pursuit files
MUST use that predicate and MUST NOT match reserved names by substring of the
path.

#### Scenario: A pursuit name contains a reserved word
- **GIVEN** a workspace pursuit at `accounts/acme-corp/pursuits/gmail-intel-rollout.md`
- **WHEN** the operator runs `fieldkit brief`, `fieldkit pipeline`, or a pursuit portfolio report
- **THEN** the pursuit SHALL be included like any other pursuit

#### Scenario: A template file sits in a pursuits directory
- **GIVEN** a workspace containing `accounts/acme-corp/pursuits/template.md`
- **WHEN** any pursuit-enumerating command scans the workspace
- **THEN** the file SHALL NOT be treated as a pursuit

### Requirement: Creation and rename reject reserved names

`fieldkit pursuit create` and `fieldkit pursuit rename` MUST refuse a target
slug whose pursuit file name would be reserved. The refusal MUST exit with the
data-error status (3), MUST name the reserved slug, and MUST NOT create,
rename, or modify any file or watcher state.

#### Scenario: Creating a pursuit named template
- **GIVEN** an initialized workspace with account `acme-corp`
- **WHEN** the operator runs `fieldkit pursuit create --account acme-corp --name template --json`
- **THEN** the command SHALL exit 3
- **AND** no file SHALL exist at `accounts/acme-corp/pursuits/template.md`
- **AND** the JSON error SHALL identify `template` as a reserved name

#### Scenario: Renaming a pursuit to a reserved name
- **GIVEN** an existing pursuit `accounts/acme-corp/pursuits/real-deal.md`
- **WHEN** the operator runs `fieldkit pursuit rename --account acme-corp --from real-deal --to gmail-intel`
- **THEN** the command SHALL exit 3
- **AND** `real-deal.md` and the watcher state files SHALL be unchanged

#### Scenario: Creating a pursuit with an ordinary name
- **GIVEN** an initialized workspace with account `acme-corp`
- **WHEN** the operator creates a pursuit named `real-deal`
- **THEN** the command SHALL succeed and the pursuit SHALL appear in forecast, health, and audit output

### Requirement: Reports disclose skipped reserved files

Portfolio reports MUST name, in their diagnostics, every reserved file they
skip in a `pursuits/` directory. This applies to forecast, health, and audit.
They MUST NOT change exit status solely because a reserved file was skipped.

#### Scenario: An existing workspace already holds a reserved-name pursuit
- **GIVEN** `accounts/acme-corp/pursuits/gmail-intel.md` with `stage: propose`
- **WHEN** the operator runs `fieldkit pursuit forecast --json`
- **THEN** the diagnostics SHALL list that path as skipped because its name is reserved
- **AND** the exit status SHALL be the same as for the workspace without that file
