# pii-guard-runtime-seeds Specification

## Purpose
Define the current behavioral contract for pii-guard-runtime-seeds, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Customer account slugs come only from private runtime sources

The PII guard SHALL build its customer account-slug detector from the union of
live account configuration and an optional
`<fieldkit_data>/pii-guard-seeds.json` file. The repository SHALL NOT contain a
real customer slug or a committed customer seed list. Approved fixture slugs
SHALL be removed after runtime sources are combined.

#### Scenario: private seed extends protection

- **GIVEN** a valid private seed containing a synthetic non-fixture slug and live account configuration is unavailable
- **WHEN** the hook scans a commit message containing that slug
- **THEN** the hook rejects the message without echoing the slug

#### Scenario: approved fixtures remain allowed

- **GIVEN** an approved fixture slug appears in either runtime source
- **WHEN** the hook scans a commit message containing that fixture
- **THEN** the fixture is not reported as an account-slug violation

### Requirement: Existing seed files satisfy a strict privacy contract

An existing seed path SHALL be an owned regular file, SHALL NOT be a symlink,
and SHALL have mode exactly `0600`. Its contents SHALL be a UTF-8 JSON array of
strings matching `^[A-Za-z0-9_-]+$`. Accepted values SHALL be normalized to
lowercase and deduplicated. The hook SHALL reject an existing seed that cannot
be securely inspected or violates this schema.

#### Scenario: insecure permissions block the hook

- **GIVEN** the seed exists with group or other permission bits
- **WHEN** either hook mode starts
- **THEN** it exits non-zero with a fixed permission diagnostic before accepting the input

#### Scenario: symlink is rejected before reading

- **GIVEN** the seed path is a symlink to any target
- **WHEN** the hook starts
- **THEN** it exits non-zero with a fixed file-type diagnostic and does not read the target

#### Scenario: malformed content blocks without disclosure

- **GIVEN** an owned `0600` seed contains invalid JSON, a non-array value, or an invalid member
- **WHEN** the hook starts
- **THEN** it exits non-zero with a fixed schema diagnostic that contains neither the file contents nor a member value

### Requirement: Missing supplemental protection is observable

An absent seed SHALL NOT disable the hook or its other PII checks. The hook SHALL
continue with live account configuration and emit a fixed degraded-state warning.
If live account configuration is also unavailable or empty, the warning SHALL
state that no runtime account-slug source is active without revealing paths or
account data.

#### Scenario: missing seed with live accounts

- **GIVEN** no seed file exists and live account configuration supplies at least one account slug
- **WHEN** the hook starts
- **THEN** it warns that supplemental seed protection is absent and still rejects those live account slugs

#### Scenario: both runtime sources are empty

- **GIVEN** no seed exists and live account configuration supplies no account slugs
- **WHEN** the hook scans otherwise clean input
- **THEN** it continues all non-account PII checks and emits a fixed warning that account-slug coverage has no active runtime source
