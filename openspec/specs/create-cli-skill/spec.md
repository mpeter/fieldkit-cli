# create-cli-skill Specification

## Purpose
Define the bundled workflow for designing consistent, automation-safe fieldkit CLI interfaces before implementation.
## Requirements
### Requirement: CLI interface work produces a fieldkit command contract

The bundled `create-cli` skill SHALL activate for a new fieldkit command or a material change to command syntax or observable behavior and SHALL produce a compact implementation-ready contract before code changes. The contract SHALL cover command tree and help, arguments/options and complete plumbing, stdout/stderr, stable machine output, canonical fieldkit exit semantics, prompt and destructive-action safety, configuration precedence, idempotence or dry-run behavior, compatibility, examples, tests, and documentation.

#### Scenario: New command design uses fieldkit contracts
- **GIVEN** an operator asks to design a new fieldkit subcommand
- **WHEN** the skill prepares the interface
- **THEN** it inspects the canonical architecture, exit-code docs, and adjacent Click patterns
- **AND** it produces grep-checkable behavior and plumbing acceptance rather than implementation code

#### Scenario: Material option change proves observable behavior
- **GIVEN** an existing command will gain an option that changes scope or output
- **WHEN** the skill specifies that change
- **THEN** it traces the option from Click parsing through domain behavior and both human and machine output
- **AND** it defines tests that fail if the option is accepted but ignored

#### Scenario: Ordinary domain edit does not trigger CLI design
- **GIVEN** a code change leaves command syntax, output, exits, prompts, and configuration behavior unchanged
- **WHEN** skills are routed for that work
- **THEN** `create-cli` is not selected solely because the code is reachable from a command

### Requirement: The skill preserves authoritative conventions and upstream provenance

The skill SHALL reference fieldkit's maintained documentation instead of creating a competing exit-code or architecture standard. It SHALL identify the adapted upstream source and pinned commit and SHALL ship the applicable MIT notice.

#### Scenario: Usage validation follows fieldkit exit semantics
- **GIVEN** a proposed command has invalid user input or Click usage errors
- **WHEN** the skill defines its failure contract
- **THEN** it points to the canonical exit documentation and specifies fieldkit status 3
- **AND** it does not repeat the upstream template's generic usage status 2

