# territory-account-identity Specification

## Purpose
Define the current behavioral contract for territory-account-identity, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Territory resolution uses configured global account identity

When an account has a syntactically valid `sf_gsg_id`, territory resolution SHALL restrict Opportunity candidates with an exact `Account.GU_Proxy_ID__c` SOSL filter and SHALL retain the configured Territory2 DeveloperName check before accepting an ID.

#### Scenario: Valid GSG identity scopes candidates
- **GIVEN** an account has a search term, expected territory DeveloperName, and valid GSG identifier
- **WHEN** fieldkit resolves its Territory2 ID
- **THEN** the Opportunity SOSL keeps the search term in `FIND`
- **AND** its `RETURNING` clause filters `Account.GU_Proxy_ID__c` by the exact GSG identifier
- **AND** only a candidate whose Territory2 DeveloperName matches the configured value is returned

#### Scenario: Missing GSG retains compatibility
- **GIVEN** an account has no configured GSG identifier
- **WHEN** fieldkit resolves its Territory2 ID
- **THEN** it uses the existing opportunity-name candidate search
- **AND** it retains the Territory2 DeveloperName match

#### Scenario: Malformed GSG cannot alter SOSL
- **GIVEN** an account has a GSG value outside the accepted identifier syntax
- **WHEN** fieldkit resolves its Territory2 ID
- **THEN** the malformed value is not interpolated into SOSL
- **AND** fieldkit warns and follows the missing-GSG compatibility path

### Requirement: Account identity accessors return stable defensive values

The configuration API SHALL expose account-scoped accessors for `sf_gsg_id` and `sf_account_ids` through `fieldkit.config`, reusing the shared cached accounts loader.

#### Scenario: Configured identity is returned
- **GIVEN** an account has a nonblank string `sf_gsg_id` and a list of account IDs
- **WHEN** callers request its identity values
- **THEN** the GSG accessor returns the stripped identifier
- **AND** the account-ID accessor returns nonblank strings in configured order with duplicates removed

#### Scenario: Malformed or missing identity is empty
- **GIVEN** the account, account mapping, GSG value, or account-ID list has the wrong shape or is absent
- **WHEN** callers request identity values
- **THEN** the GSG accessor returns `None`
- **AND** the account-ID accessor returns an empty list

### Requirement: Dead opportunity territory is absent from the active pursuit contract

`sf_opportunity_territory` SHALL be absent from the Pydantic field set, generated Salesforce field set, and JSON schema while legacy documents containing it remain loadable and retain the unknown key during unrelated frontmatter writes.

#### Scenario: New contract does not advertise the dead field
- **WHEN** code inspects `PursuitFrontmatter.model_fields`, `SF_FIELD_NAMES`, or the pursuit JSON schema
- **THEN** `sf_opportunity_territory` is absent

#### Scenario: Legacy file survives an unrelated round trip
- **GIVEN** an existing pursuit file contains `sf_opportunity_territory`
- **WHEN** fieldkit loads it and writes an unrelated modeled field
- **THEN** loading succeeds
- **AND** the original legacy key and value remain in the file
