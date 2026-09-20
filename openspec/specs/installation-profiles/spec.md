# installation-profiles Specification

## Purpose
Define the current behavioral contract for installation-profiles, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Every published dependency has one profile owner

The project MUST maintain a machine-readable ownership manifest mapping each runtime requirement,
import root, and optional command group to base, `google`, `llm`, `web`, or `chrome-auth`. The `all`
profile MUST equal the union of optional profiles, and validation MUST fail on missing, ambiguous,
or metadata-divergent ownership.

#### Scenario: Dependency metadata drifts
- **GIVEN** a requirement or import root is added, removed, or moved
- **WHEN** profile validation runs
- **THEN** an unowned or multiply owned entry fails with its manifest key and expected profile
- **AND** the `all` union is recomputed and compared without network access

### Requirement: Base installation is independently useful

The base distribution MUST install without Google, LiteLLM/Vertex, FastAPI/Uvicorn, or browser
credential-store stacks and MUST provide the documented offline first-success workflow. Listing
top-level help and base commands MUST NOT import optional SDKs.

#### Scenario: User installs only the base wheel
- **GIVEN** an empty environment containing the base wheel and no optional extras
- **WHEN** the user runs version, help, minimal initialization, general diagnostics, and the
  documented offline operation
- **THEN** every base command succeeds with its documented exit code
- **AND** no network, credential, organization configuration, or source checkout is required

### Requirement: Missing optional profiles are actionable without masking defects

Invoking a command whose declared profile is absent MUST emit the profile name and copyable
`fieldkit-cli[PROFILE]` installation guidance, MUST exit 3, and MUST NOT show a traceback by default.
Import failures after declared roots are present MUST NOT be reclassified as missing-profile errors.

#### Scenario: Google command is selected from a base install
- **GIVEN** `fieldkit-cli` is installed without the `google` profile
- **WHEN** a Google-owned command is invoked
- **THEN** the command identifies `google` as required and prints pip and uv installation forms
- **AND** exits 3 without importing the Google SDK

#### Scenario: Optional command contains a broken internal import
- **GIVEN** every declared import root for the command profile is available
- **WHEN** importing the command fails for another reason
- **THEN** normal defect handling applies
- **AND** the failure is not presented as an installation-profile omission
