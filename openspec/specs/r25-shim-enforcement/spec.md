# r25-shim-enforcement Specification

## Purpose
Define commit-time enforcement that prevents retired compatibility surfaces
from returning through new shim files or edits to existing modules.
## Requirements
### Requirement: Modified modules cannot restore retired error re-exports

The commit-time shim guard SHALL inspect the staged Git content of modified or
renamed non-package Python modules and reject redundant `X as X` imports for
error symbols whose former compatibility surfaces were deliberately removed
under R25.

#### Scenario: Retired surface is restored in place

- **GIVEN** an existing non-`__init__.py` Python module is staged as modified
- **AND** it imports a protected error symbol using `X as X`
- **WHEN** the shim guard runs
- **THEN** it reports the file, line, and symbol
- **AND** exits with a blocking status

#### Scenario: Index and working tree differ

- **GIVEN** a protected re-export is present in the staged blob
- **AND** the unstaged working-tree copy does not contain it
- **WHEN** the shim guard runs
- **THEN** it inspects the staged blob and blocks the commit

#### Scenario: Re-export is added during a rename

- **GIVEN** an existing Python module is renamed and gains a protected
  redundant alias in the same staged change
- **WHEN** the shim guard runs
- **THEN** it inspects the rename destination and blocks the commit

#### Scenario: Legitimate use import remains valid

- **GIVEN** a modified module imports a protected error symbol without a
  redundant alias or uses it in an exception binding
- **WHEN** the shim guard runs
- **THEN** that use does not produce a modified-file re-export violation

#### Scenario: Package aggregator remains valid

- **GIVEN** a staged package `__init__.py` uses an explicit `X as X` export
- **WHEN** the shim guard selects modified files
- **THEN** the package aggregator is excluded from this check

### Requirement: Shim violations are reported together

The shim guard SHALL retain its whole-file check for staged additions and SHALL
report added-file shims and modified-file error re-exports found in the same run
before exiting.

#### Scenario: Both violation classes are staged

- **GIVEN** a new whole-file shim and a modified error re-export are staged
- **WHEN** the shim guard runs
- **THEN** its diagnostic identifies both offenders
- **AND** it exits with a blocking status
