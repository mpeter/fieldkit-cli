# skill-command-reference-integrity Specification

## Purpose
Define the current behavioral contract for skill-command-reference-integrity, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: executable fieldkit command references resolve

The skill-integrity gate MUST validate `fieldkit` command paths at the start of shell-fence lines and
inline code spans in both canonical skill roots against the shared in-process Click command tree.

#### Scenario: valid command with arguments or placeholders

- **GIVEN** a skill cites a real top-level, grouped, or nested command followed by arguments, flags, or placeholders
- **WHEN** skill integrity runs
- **THEN** the reference passes based only on its longest valid command path

#### Scenario: stale command path

- **GIVEN** a skill's executable code cites a command path absent from the Click tree
- **WHEN** skill integrity runs
- **THEN** it emits an error naming the file, line, and unknown path

#### Scenario: prose and embedded search text

- **GIVEN** prose, a source-language string, or another shell command mentions `fieldkit`
- **WHEN** skill integrity runs
- **THEN** the mention is outside the extraction boundary

### Requirement: invalid-example exceptions are narrow and accountable

The exact `<!-- fieldkit-command-check: ignore-next -->` marker MUST suppress only the next extracted
invocation, and the gate MUST error when no later invocation consumes it.

#### Scenario: intentional invalid example

- **GIVEN** an ignore-next marker precedes an extracted invalid invocation
- **WHEN** skill integrity runs
- **THEN** that invocation is suppressed and later invocations remain subject to validation

#### Scenario: unused exception

- **GIVEN** an ignore-next marker has no later extracted invocation
- **WHEN** skill integrity runs
- **THEN** it emits an error for the unused marker

### Requirement: canonical skill roots are scanned once

The gate MUST scan `.opencode/skills` and `src/fieldkit/skills` and MUST NOT separately follow the
`.claude/skills` symlink.

#### Scenario: the Claude bridge points to OpenCode skills

- **GIVEN** `.claude/skills` resolves to `.opencode/skills`
- **WHEN** skill integrity runs
- **THEN** each OpenCode skill is scanned once from its canonical root
