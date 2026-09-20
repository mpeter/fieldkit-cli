# issue-module-domain-set Specification

## Purpose
Define the current behavioral contract for issue-module-domain-set, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: current fieldkit domains are valid issue modules

The issue CLI MUST accept `companion`, `config`, `contact`, `driver`, `health`, `meeting`, and `web`
as module values in addition to every previously supported module.

#### Scenario: create an issue for a current domain

- **GIVEN** an issue is created with any newly supported current domain
- **WHEN** module validation runs
- **THEN** the value is accepted and used as the issue's module label

#### Scenario: reject an unknown module

- **GIVEN** an issue is created with a value outside the canonical set
- **WHEN** module validation runs
- **THEN** the command rejects it and reports the complete valid set

### Requirement: raise-issue guidance matches runtime validation

The raise-issue skill MUST document the same module values returned by the runtime module authority.

#### Scenario: module support changes

- **GIVEN** the runtime module set or the skill Field Guide changes
- **WHEN** the module-parity regression test runs
- **THEN** it fails unless both surfaces contain the same values
