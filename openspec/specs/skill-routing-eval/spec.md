# skill-routing-eval Specification

## Purpose
Define the current behavioral contract for skill-routing-eval, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Routing fixtures define expected top choices

fieldkit SHALL package a versioned fixture set whose cases have a unique identifier,
an operator utterance, and one expected bundled skill. The fixture loader SHALL reject
malformed cases, duplicate identifiers, unsupported versions, and expected skills that
are absent from the current corpus.

#### Scenario: Valid fixtures load

- **GIVEN** a version-1 fixture file with unique complete cases
- **AND** every expected skill exists in the bundled corpus
- **WHEN** routing evaluation loads the fixtures
- **THEN** it returns typed cases in fixture order

#### Scenario: A fixture names a removed skill

- **GIVEN** a fixture expects a skill absent from the bundled corpus
- **WHEN** routing evaluation validates the fixtures
- **THEN** it fails as a data error
- **AND** it does not report the corpus as passing

### Requirement: Every utterance is judged against the complete skill corpus

For each routing fixture, fieldkit SHALL provide the model every bundled skill name and
non-empty trigger description and require exactly one top-selected skill. It SHALL NOT
provide the expected skill answer key in the prompt. Utterances and descriptions SHALL
be delimited as untrusted data.

#### Scenario: Top choice matches the fixture

- **GIVEN** a valid fixture and a corpus of bundled skill descriptions
- **WHEN** `fieldkit skill eval --routing` selects the fixture's expected skill
- **THEN** the case passes
- **AND** the command records the selected skill, reason, model, and corpus size

#### Scenario: A competing skill ranks first

- **GIVEN** a valid fixture and the complete bundled corpus
- **WHEN** the model selects a skill other than the expected skill
- **THEN** the case fails
- **AND** the command exits 1 after reporting the mismatch

#### Scenario: Model output cannot identify a corpus skill

- **GIVEN** a valid fixture and corpus
- **WHEN** the model response remains malformed or names an unknown skill after one retry
- **THEN** routing evaluation fails as a data error
- **AND** it does not convert that response into a mismatch or pass

### Requirement: Routing evaluation supports deterministic plumbing checks

Routing evaluation SHALL support the existing `NO_LLM=1` mode and JSON output. Stub
results SHALL exercise fixture and corpus validation without making a model call and
SHALL exit 0 when that validation succeeds.

#### Scenario: Stub mode validates local inputs

- **GIVEN** `NO_LLM=1` and valid fixtures and corpus
- **WHEN** `fieldkit skill eval --routing --json` runs
- **THEN** no model call occurs
- **AND** every result is visibly marked as a passing stub
