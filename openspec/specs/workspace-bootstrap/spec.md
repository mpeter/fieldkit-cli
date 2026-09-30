# workspace-bootstrap Specification

## Purpose
Define how `fieldkit init` creates a supported workspace interactively, minimally, or from validated unattended input while preserving unrelated operator files.

## Requirements
### Requirement: Initialization supports validated unattended input

`fieldkit init` SHALL accept a YAML answers file containing the same managed values as the interactive wizard. It SHALL validate the complete document before writing any artifact, SHALL reject unknown keys, and SHALL run without reading stdin or requesting confirmation when the file is supplied.

#### Scenario: Valid answers initialize without prompts

- **GIVEN** a mapping-form answers file with non-empty `name`, `email`, and `data_dir` values
- **WHEN** the operator runs `fieldkit init --answers FILE`
- **THEN** initialization completes without reading stdin or requesting confirmation
- **AND** the managed config and workspace artifacts contain the supplied values

#### Scenario: Invalid answers cause no partial initialization

- **GIVEN** an answers file with a malformed root, unknown key, missing or empty required value, wrong value type, unsafe account name, OAuth secret without a client ID, or invalid ShadowBot identifier
- **WHEN** the operator runs `fieldkit init --answers FILE`
- **THEN** the command exits with the canonical data-error status 3
- **AND** no workspace or global configuration artifact is written

#### Scenario: Answers file is unsafe or contains sensitive malformed input

- **GIVEN** an answers file is not a stable regular UTF-8 file, exceeds 1 MiB, or contains malformed YAML
- **WHEN** the operator runs `fieldkit init --answers FILE`
- **THEN** the command MUST exit with data-error status 3 before initialization
- **AND** file-read and parse-error diagnostics MUST NOT reflect the path or input contents

### Requirement: Optional credentials retain exact dotenv values

Initialization SHALL serialize optional OAuth values through the canonical
dotenv writer into a private mode-0600 file. The generated file SHALL be dotenv
data rather than shell code. Loading SHALL preserve literal values without
variable interpolation. NUL and carriage returns SHALL be rejected before
workspace artifacts are created.

#### Scenario: Credential includes literal variables and multiline text

- **GIVEN** a supported credential includes dollar signs, quotes, backslashes, Unicode, or LF newlines
- **WHEN** initialization writes the dotenv file and fieldkit loads it
- **THEN** the loaded value MUST exactly match the supplied credential
- **AND** staging and published files MUST have mode 0600

#### Scenario: Unsupported credential characters do not partially initialize

- **GIVEN** an OAuth credential contains NUL or a carriage return
- **WHEN** initialization runs from valid answers
- **THEN** the command MUST exit with data-error status 3
- **AND** no workspace artifacts or global configuration MUST be written

### Requirement: Initialization preserves invalid existing user configuration

Initialization SHALL reject unreadable, malformed, non-mapping, or symlinked
existing user configuration rather than treating it as empty replacement state.
It SHALL re-read configuration before writing a merged global configuration.

#### Scenario: Existing global configuration is invalid

- **GIVEN** existing user configuration is malformed or cannot be read as a mapping
- **WHEN** the operator initializes from a valid answers file
- **THEN** initialization MUST exit with data-error status 3 before creating workspace artifacts
- **AND** the existing configuration bytes MUST remain unchanged

#### Scenario: Configuration becomes invalid after defaults were read

- **GIVEN** initialization read valid defaults
- **AND** user configuration becomes malformed before the global write
- **WHEN** the writer re-reads the configuration
- **THEN** it MUST reject the write rather than replace the malformed file

### Requirement: Initialization preflights workspace destinations

In a stable workspace directory namespace, initialization SHALL reject existing
child destinations that redirect through symlinks or have incompatible file
types before producing workspace artifacts. The selected workspace root MAY be
an alias to an existing directory but SHALL NOT be a dangling symlink. This
contract SHALL NOT claim concurrent-rename containment or rollback
of earlier artifacts after a later write failure.

#### Scenario: Account destination redirects outside the selected workspace

- **GIVEN** an existing account directory or account note is a symlink
- **WHEN** initialization selects that account
- **THEN** the command MUST exit with data-error status 3 before writing artifacts
- **AND** the redirected destination MUST remain unchanged

#### Scenario: Minimal directory redirects outside the selected workspace

- **GIVEN** the workspace config, accounts, or data directory is a symlink
- **WHEN** minimal initialization runs
- **THEN** the command MUST exit with data-error status 3 before writing artifacts
- **AND** existing workspace contents MUST remain unchanged

#### Scenario: Selected workspace is a dangling alias

- **GIVEN** the selected workspace is a symlink whose target does not exist
- **WHEN** minimal or answers-file initialization runs
- **THEN** the command MUST exit with data-error status 3
- **AND** neither the target nor global configuration MUST be created

### Requirement: Installed initialization does not invent a source checkout

The wizard SHALL use canonical source-layout discovery instead of Git subprocess
probing or package-parent guesses. When no identified fieldkit source tree is
available, it SHALL NOT introduce a `fieldkit_root` override. Existing explicit
configuration SHALL remain preserved.

#### Scenario: Installed-package initialization without an explicit override

- **GIVEN** the wizard is loaded from an installed package rather than `src/fieldkit`
- **AND** no existing `fieldkit_root` override is configured
- **WHEN** initialization writes global configuration
- **THEN** the configuration MUST NOT contain a guessed `fieldkit_root`

#### Scenario: Installed initialization retains explicit source configuration

- **GIVEN** an existing `fieldkit_root` override is configured
- **WHEN** an installed-package wizard updates other managed values
- **THEN** the existing override MUST remain unchanged

### Requirement: Initialization creates only supported workspace scaffolding

Initialization SHALL create its supported workspace directories, account index,
and selected managed configuration. It SHALL NOT create clocks, engines,
people, or watchlist JSON scaffolds with no shipped consumer. Interactive and
answers-file initialization MAY additionally create the selected identity,
account notes, and explicitly supplied integration configuration.

#### Scenario: Fresh workspace omits unused briefing configuration

- **GIVEN** the selected workspace has no existing briefing JSON files
- **WHEN** initialization writes its artifacts
- **THEN** its supported account configuration is available
- **AND** no `config/clocks.json`, `config/engines.json`, `config/people.json`, or `config/watchlist.json` is created

#### Scenario: Rerun preserves unrelated operator data

- **GIVEN** unrelated operator-authored files already exist in the workspace
- **WHEN** initialization is rerun
- **THEN** those files remain byte-for-byte unchanged
- **AND** initialization does not fill missing unused briefing scaffolds
