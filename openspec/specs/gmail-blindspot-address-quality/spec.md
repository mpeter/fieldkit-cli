# gmail-blindspot-address-quality Specification

## Purpose
Define the current behavioral contract for gmail-blindspot-address-quality, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Suspected masked addresses use a conservative account-scoped signal

The system SHALL classify a blindspot address as `suspected-masked` only when its domain exactly matches a configured domain for the selected account and its local part consists of at least two name-like segments followed by exactly four lowercase ASCII alphanumeric characters containing at least one letter.

#### Scenario: Same-account masked shape is classified

- **GIVEN** `acme-corp.com` is configured for account `acme-corp`
- **WHEN** a blindspot address is `alex.taylor.q7zm@acme-corp.com`
- **THEN** it is classified as `suspected-masked`
- **AND** its stable reason identifies the account-domain suffix heuristic

#### Scenario: Numeric year suffix remains ordinary

- **GIVEN** `acme-corp.com` is configured for account `acme-corp`
- **WHEN** a blindspot address ends in `.2026@acme-corp.com`
- **THEN** it is not classified as suspected masked data

#### Scenario: Lookalike from another domain remains ordinary

- **GIVEN** `acme-corp.com` is configured for account `acme-corp`
- **WHEN** a blindspot address is `alex.taylor.q7zm@example.net`
- **THEN** it is not classified as suspected masked data

#### Scenario: Account domains are unavailable

- **WHEN** the selected account has no valid configured domains
- **THEN** no address is classified by this heuristic
- **AND** existing blindspot results remain available

### Requirement: Default output separates suspected records without erasing them

The default human and JSON actionable item set SHALL exclude suspected masked addresses. Human output SHALL report the set-aside count and recovery option. JSON SHALL include the suspected records, count, and stable reason as additive metadata.

#### Scenario: Default human output contains suspected records

- **WHEN** one ordinary and one suspected masked address pass the existing blindspot filters
- **THEN** the table shows the ordinary address as actionable
- **AND** the suspected address is absent from the default table
- **AND** output reports one set-aside address and names `--include-suspected`

#### Scenario: JSON preserves the suspected record

- **WHEN** the same result set is requested with `--json`
- **THEN** `items` contains the ordinary address
- **AND** `suspected_items` contains the suspected address with `quality_reason`
- **AND** `suspected_count` equals the length of `suspected_items`
- **AND** `count` equals the length of `items`

### Requirement: Operators can inspect suspected records explicitly

`gmail query blindspots` SHALL accept `--include-suspected`. When selected, suspected records SHALL appear in the main items and SHALL remain visibly annotated.

#### Scenario: Human inspection is requested

- **WHEN** the operator passes `--include-suspected`
- **THEN** the table includes suspected records
- **AND** each suspected record is visibly marked as suspected masked data

### Requirement: Reports use the same address-quality decision

Generated Gmail intelligence SHALL exclude suspected masked records from contact and champion analysis and SHALL state how many were set aside.

#### Scenario: Gmail intelligence contains a suspected address

- **WHEN** report generation receives one ordinary and one suspected masked blindspot tuple
- **THEN** only the ordinary address participates in contacts and champion analysis
- **AND** the report states that one suspected masked address was set aside

### Requirement: Classification precedes the public result limit

Suspected records SHALL NOT consume the actionable result limit in default output.

#### Scenario: Recent suspected record precedes an ordinary record

- **WHEN** a suspected record sorts ahead of an ordinary record and the public limit is one
- **THEN** the ordinary record remains in `items`
- **AND** the suspected record remains in the separate suspected metadata
