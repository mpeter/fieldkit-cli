# distribution-identity Specification

## Purpose
Define the current behavioral contract for distribution-identity, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Distribution command and import identities are explicit

The build metadata MUST publish version `1.0.0` under distribution name `fieldkit-cli`. It MUST
continue to install the `fieldkit` console command and `fieldkit` Python import package. Installed
version reporting MUST query `fieldkit-cli` metadata as the single version authority.

#### Scenario: Installed user asks for the version
- **GIVEN** the release-candidate wheel is installed in an empty environment
- **WHEN** the user runs `fieldkit --version` and queries Python distribution metadata
- **THEN** both report `1.0.0` for `fieldkit-cli`
- **AND** importing `fieldkit` resolves from that installed wheel

#### Scenario: Developer runs from an unpackaged checkout
- **GIVEN** source is executed without installed `fieldkit-cli` metadata
- **WHEN** the version helper is called
- **THEN** it returns the documented development fallback
- **AND** it does not query the unrelated `fieldkit` distribution

### Requirement: Core metadata is publication-complete

Wheel and source-distribution metadata MUST declare Markdown project description, Apache-2.0 SPDX
license expression and license file, maintainers, Python requirement, classifiers, keywords, and
stable project URLs. Both artifacts MUST pass strict metadata validation without warnings.

#### Scenario: Release artifacts are inspected
- **GIVEN** wheel and source distribution built from the same clean revision
- **WHEN** strict metadata validation runs
- **THEN** both artifacts pass without warnings
- **AND** their name, version, license, Python requirement, and URLs agree
