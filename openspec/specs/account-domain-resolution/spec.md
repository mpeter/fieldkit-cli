# account-domain-resolution Specification

## Purpose
Define the current behavioral contract for account-domain-resolution, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Unique account domains resolve case-insensitively

The account-domain resolver MUST normalize non-empty string domains to lowercase and MUST map a domain claimed by exactly one distinct account slug to that slug.

#### Scenario: Unique mixed-case domain
- **GIVEN** one account claims `Example.COM`
- **WHEN** the account-domain map is built
- **THEN** the map contains `example.com` assigned to that account slug

#### Scenario: Same account repeats a normalized domain
- **GIVEN** one account declares the same domain more than once with different casing
- **WHEN** the account-domain map is built
- **THEN** the normalized domain remains mapped to that account
- **AND** no ambiguity warning is emitted for that domain

### Requirement: Conflicting domain claims fail closed

The resolver MUST omit a normalized domain claimed by more than one distinct account slug, regardless of account iteration order, and MUST emit exactly one warning per ambiguous domain naming the domain and all claimants observed when ambiguity is first detected.

#### Scenario: Two accounts claim one domain
- **GIVEN** two distinct account slugs claim the same normalized domain
- **WHEN** the account-domain map is built
- **THEN** the domain is absent from the returned map
- **AND** one warning names the normalized domain and both slugs

#### Scenario: Account order is reversed
- **GIVEN** the same conflicting claims are supplied in the opposite account order
- **WHEN** the account-domain map is built
- **THEN** the returned map is unchanged
- **AND** the domain remains absent

#### Scenario: Later claimant follows an established conflict
- **GIVEN** a normalized domain has already been marked ambiguous
- **WHEN** another account claims it
- **THEN** the domain remains absent
- **AND** no repeated warning is emitted for that domain

### Requirement: Enrichment does not override from an ambiguous domain

Contact enrichment MUST NOT assign an account through the domain map when that domain has conflicting configured claimants.

#### Scenario: External contact has an ambiguous configured domain
- **GIVEN** two accounts claim the contact's normalized email domain
- **WHEN** the contact batch is enriched
- **THEN** domain mapping does not override the enriched record's account
- **AND** the existing unmatched external-domain behavior marks confidence `LOW` and source `inferred-context`
