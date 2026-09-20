# claude-skills-symlink-integrity Specification

## Purpose
Define the current behavioral contract for claude-skills-symlink-integrity, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: the Claude skills bridge is a canonical relative symlink

The Claude sync gate MUST succeed only when `.claude/skills` is a symbolic link whose raw target is
exactly `../.opencode/skills`.

#### Scenario: canonical bridge

- **GIVEN** `.claude/skills` is a symlink to `../.opencode/skills`
- **WHEN** the sync gate runs
- **THEN** symlink validation succeeds and ordinary agent/command mirror checks continue

#### Scenario: missing or materialized bridge

- **GIVEN** `.claude/skills` is absent or is a real directory
- **WHEN** the sync gate runs in check or sync mode
- **THEN** it exits 1 with a diagnostic that identifies the required link
- **AND** it does not delete, replace, or modify the path

#### Scenario: misdirected bridge

- **GIVEN** `.claude/skills` is a symlink with any other raw target, including an absolute path to the same directory
- **WHEN** the sync gate runs
- **THEN** it exits 1 and reports the expected relative target
