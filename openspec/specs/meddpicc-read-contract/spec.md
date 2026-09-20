# meddpicc-read-contract Specification

## Purpose
Define the current behavioral contract for meddpicc-read-contract, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: ClosePlan reads expose every canonical MEDDPICC element

`fieldkit sf meddpicc OPP_ID` SHALL return the eight canonical MEDDPICC elements in deterministic order while preserving each fetched question's name, score, and answer text. Questions whose category prefix is not recognized SHALL remain visible as unmapped data.

#### Scenario: Populated scorecard is normalized without inventing aggregates
- **GIVEN** a ClosePlan scorecard contains one or more questions for canonical MEDDPICC categories
- **WHEN** the command renders human or JSON output
- **THEN** every canonical element appears in order with its question-level values
- **AND** the command does not synthesize an undocumented category score

#### Scenario: Template drift remains observable
- **GIVEN** a ClosePlan question has an unknown or missing category prefix
- **WHEN** the scorecard is normalized
- **THEN** the question appears in the unmapped collection
- **AND** no fetched record is silently discarded

### Requirement: Population state distinguishes absent data from zero

Each canonical element SHALL report exactly one of `unpopulated`, `answered_unscored`, `scored_zero`, or `scored`. `gaps` SHALL contain only elements with no score and no nonblank answer data.

#### Scenario: Explicit zero is retained as a score
- **GIVEN** an element has a numeric score of zero
- **WHEN** the command classifies it
- **THEN** its state is `scored_zero`
- **AND** it is absent from `gaps`

#### Scenario: Missing and answer-only data remain distinct
- **GIVEN** one element has no score or answer and another has answer text but a null score
- **WHEN** the command classifies them
- **THEN** the first is `unpopulated` and appears in `gaps`
- **AND** the second is `answered_unscored` and does not appear in `gaps`

### Requirement: Named-party evidence is available in both output modes

Champion and Economic Buyer answer text SHALL be preserved and displayed in human output and serialized in JSON without heuristic identity extraction.

#### Scenario: ClosePlan contains a named Champion
- **GIVEN** the Champion answer names a person
- **WHEN** the command renders the scorecard
- **THEN** the full answer text is visible under Champion in human output
- **AND** the same text is present in the Champion element's JSON questions

### Requirement: No-ClosePlan remains a successful read result

An opportunity without a linked ClosePlan SHALL exit successfully and identify that state without confusing it with a populated scorecard whose elements are blank.

#### Scenario: Opportunity has no linked scorecard
- **GIVEN** Salesforce returns no `TSPC__Deal__c` for the opportunity
- **WHEN** the command runs
- **THEN** human output states that no ClosePlan is configured and exits 0
- **AND** JSON reports `has_closeplan: false` with empty elements, gaps, and unmapped collections
