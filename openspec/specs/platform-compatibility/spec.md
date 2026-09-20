# platform-compatibility Specification

## Purpose
Define the current behavioral contract for platform-compatibility, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Support claims are backed by installed-package evidence

The compatibility policy MUST distinguish supported core, integration-specific, experimental, and
unsupported environments. Every supported Python/OS pair MUST pass the same focused base-artifact
contract before documentation may claim support.

#### Scenario: Supported matrix cell is evaluated
- **GIVEN** CPython 3.11, 3.12, 3.13, or 3.14 on a declared Ubuntu or macOS environment
- **WHEN** compatibility smoke installs the release-candidate artifact
- **THEN** version, help, minimal initialization, general diagnostics, offline first success, and
  sanctioned file-write behavior pass
- **AND** evidence records the Python, OS, source revision, and artifact digest

#### Scenario: Fedora or RHEL-family behavior is evaluated
- **GIVEN** the declared Fedora/RHEL-compatible container profile
- **WHEN** the focused core smoke runs
- **THEN** it passes the same portable-core contract
- **AND** Linux-only integrations are reported separately from core support

#### Scenario: Windows has not met the contract
- **GIVEN** Windows clean-install, path, subprocess, and file-write evidence is absent or failing
- **WHEN** compatibility documentation is generated or checked
- **THEN** Windows is labeled experimental
- **AND** it is not included in supported-platform claims

### Requirement: Compatibility runner is reusable by later workflows

The smoke runner and result schema MUST operate locally and in GitHub-hosted CI without private
credentials or repository write permission. The CI/security track MAY compose it into required
checks without duplicating the compatibility policy.

#### Scenario: Fork workflow invokes compatibility smoke
- **GIVEN** an untrusted fork revision and a built candidate wheel
- **WHEN** a hosted runner executes the compatibility command
- **THEN** it requires no secrets, private service, or write-capable token
- **AND** produces bounded diagnostic evidence suitable for artifact retention
