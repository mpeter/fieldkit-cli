# sf-component-services Specification

## Purpose
Define the current behavioral contract for sf-component-services, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Component lines use the shared Salesforce family contract

fieldkit SHALL classify quote lines through the single `FAMILY_BUCKETS` table and
`bucket_for_family()` implementation in `fieldkit.sf.components`. TAM SHALL map to
M000114, Learning and Consulting SHALL map to M000148, and unmatched families SHALL
remain Product with no services comp measure. Bucket ordering used by component and
go-live output SHALL also have one domain definition.

#### Scenario: Every supported family is classified

- **GIVEN** quote lines from TAM, Training, Consulting, and an unmatched product family
- **WHEN** fieldkit classifies and sorts them
- **THEN** it returns TAM, Learning, Consulting, and Product in shared bucket order
- **AND** their comp-measure codes are M000114, M000148, M000148, and empty respectively

### Requirement: Operators can inspect opportunity components

`fieldkit sf components <OPP_NUMBER_OR_ID>` SHALL resolve a 15/18-character
Salesforce Opportunity id or 5–12 digit Opportunity Number, fetch all quote lines
through `fetch_opp_component_lines()`, and render stable grouped human output.
`--json` SHALL emit sorted `ComponentLine` records. Both forms SHALL preserve null
source fields rather than infer replacements.

The ascending sort key SHALL be `(bucket_rank, quote_id, line_id_is_null, line_id,
sku_is_null, sku, product_family_is_null, product_family, net_price_is_null,
net_price, quantity_is_null, quantity, unit_price_is_null, unit_price)`, with each
nullness flag sorting populated values before null values.

#### Scenario: An opportunity contains multiple component families

- **GIVEN** a resolved opportunity with TAM, Learning, Consulting, and Product lines
- **WHEN** the operator runs `fieldkit sf components <OPP_NUMBER_OR_ID>`
- **THEN** the human view groups every line by shared bucket order
- **AND** shows quote id, SKU, family, net price, and comp-measure code

#### Scenario: JSON component output is requested

- **GIVEN** the same resolved opportunity
- **WHEN** the operator adds `--json`
- **THEN** stdout contains the sorted typed component records
- **AND** no progress text or generated timestamp is mixed into stdout

#### Scenario: Salesforce reverses otherwise identical input

- **GIVEN** the same component records returned once in forward order and once in reverse order
- **WHEN** fieldkit renders either result
- **THEN** both sorted record sequences are identical
- **AND** populated values precede null values at each nullable tie-breaker

#### Scenario: An opportunity has no quote lines

- **GIVEN** a resolved opportunity with no quotes or quote lines
- **WHEN** either output mode runs
- **THEN** human output reports no component lines or JSON emits an empty list
- **AND** the command exits 0

### Requirement: Component command failures use the central exit contract

Salesforce authentication failures from component resolution or fetching SHALL reach
`cli_main()`. Invalid or unresolved opportunity references SHALL be data errors.

#### Scenario: Salesforce authentication fails

- **GIVEN** an expired Salesforce session
- **WHEN** the component command resolves or fetches an opportunity
- **THEN** the authentication exception reaches the central mapper
- **AND** the process exits 2

#### Scenario: Opportunity reference is invalid or absent

- **GIVEN** a value that is neither a supported id nor number, or resolves to no record
- **WHEN** the component command runs
- **THEN** it reports the opportunity was not found
- **AND** exits 3

### Requirement: Listview can use quote-line services qualification

`fieldkit sf listview --services-only` SHALL perform broad account opportunity
discovery without `_SERVICES_FILTER`, then qualify each candidate through the shared
component-line walk. An opportunity SHALL qualify iff any line maps to TAM, Learning,
or Consulting. Without the flag, listview SHALL preserve its existing rollup-filtered
path and perform no component walks. Broad discovery SHALL expose whether the
2,000-record Salesforce SOSL ceiling may have truncated the result; exactly 2,000
returned records SHALL be treated as capped rather than complete.

#### Scenario: TAM-only opportunity is included

- **GIVEN** a candidate opportunity whose rollups contain no Consulting or Training
  revenue but whose quote lines include `SUPPORT - TAM`
- **WHEN** listview runs with `--services-only`
- **THEN** the opportunity enters the normal listview sync flow
- **AND** the shared classifier identifies its M000114 TAM component

#### Scenario: Product-only opportunity is excluded

- **GIVEN** a candidate whose quote lines contain only unmatched Product families
- **WHEN** listview runs with `--services-only`
- **THEN** the opportunity is not synced
- **AND** the exclusion is not counted as an error

#### Scenario: Default listview remains unchanged

- **WHEN** listview runs without `--services-only`
- **THEN** Salesforce discovery still applies `_SERVICES_FILTER`
- **AND** no quote-line component walk occurs

### Requirement: Services scans are bounded and honest about partial results

`--limit` SHALL use `click.IntRange(min=1)`, default to 50, and apply only to
`--services-only`. Range and mode validation SHALL finish before authentication or
Salesforce access. fieldkit SHALL refuse to walk an account whose broad candidate
count exceeds the selected limit, report the exact count and an actionable rerun
command, continue other accounts where applicable, and exit 1. During an accepted
scan, non-auth opportunity walk failures SHALL be reported and skipped while
remaining candidates continue; any such failure SHALL produce exit 1. fieldkit SHALL
also refuse a capped candidate set before any component walk, state that the total is
unknown, avoid recommending a numeric limit, continue other accounts where
applicable, and exit 1.

#### Scenario: Candidate count exceeds the default bound

- **GIVEN** broad discovery returns 51 candidates
- **WHEN** the operator runs `sf listview --services-only` with the default limit
- **THEN** fieldkit performs zero component walks for that account
- **AND** reports a rerun with `--limit 51`
- **AND** exits 1

#### Scenario: Salesforce returns its 2,000-record maximum

- **GIVEN** broad discovery returns exactly 2,000 candidates and marks the result capped
- **WHEN** the operator runs `sf listview --services-only --limit 2000`
- **THEN** fieldkit performs zero component walks for that account
- **AND** reports that Salesforce may have truncated an unknown total
- **AND** recommends narrowing the account selection without claiming an exact count or numeric rerun limit
- **AND** exits 1

#### Scenario: One component walk fails

- **GIVEN** three candidates within the bound and the second walk raises a non-auth API error
- **WHEN** listview applies component qualification
- **THEN** it reports and skips the second candidate, evaluates the third, and renders the summary
- **AND** exits 1

#### Scenario: Limit is used without line-aware mode

- **WHEN** the operator passes `--limit` without `--services-only`
- **THEN** Click reports a usage error
- **AND** the root handler maps it to exit 3 before Salesforce access

#### Scenario: Limit is zero or negative

- **WHEN** the operator passes `--services-only --limit 0` or `--services-only --limit -1`
- **THEN** Click reports an `IntRange(min=1)` usage error
- **AND** the root handler maps it to exit 3 before authentication or Salesforce access
