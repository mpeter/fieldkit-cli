# sf-acv-anchor Specification

## Purpose
Define the current behavioral contract for sf-acv-anchor, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Contract type is derived from quote-line product family, case-insensitively

The system SHALL derive an opportunity's contract type from its quote lines'
`SBQQ__ProductFamily__c` values via the related-list walk in
`fieldkit.sf.components` (the CLM contract-type field is not REST-reachable —
the raw child GET returns `403 TXN_SECURITY_NO_ACCESS`, and SOQL is blocked by
org TXN policy). An opportunity SHALL classify as `fixed_price` iff any quote
line's family, after whitespace-trimming and case normalization, equals the
canonical `FIXED_PRICE_FAMILY` constant (`CONSULTING - FIXED PRICE`); the match
MUST be case-insensitive, consistent with `bucket_for_family()`. All other opportunities — T&M, prepaid credits, non-consulting, or no
quote lines — SHALL classify as `standard`.

#### Scenario: canonical family string classifies as fixed-price
- **GIVEN** an opportunity whose quote lines include one with
  `SBQQ__ProductFamily__c == "CONSULTING - FIXED PRICE"`
- **WHEN** `opp_contract_type(client, opp_id)` is evaluated
- **THEN** it returns `"fixed_price"`

#### Scenario: case and whitespace variants still classify as fixed-price
- **GIVEN** an opportunity whose only consulting quote line has
  `SBQQ__ProductFamily__c == "Consulting - Fixed Price "` (mixed case, trailing
  space)
- **WHEN** `opp_contract_type(client, opp_id)` is evaluated
- **THEN** it returns `"fixed_price"`, not `"standard"` — casing/whitespace
  variance in the live org value MUST NOT silently disable the re-anchor

#### Scenario: other consulting families are not fixed-price
- **GIVEN** an opportunity whose quote lines carry only
  `CONSULTING - TIME & MATERIALS` and/or `CONSULTING - PREPAID CREDITS`
  families (any casing)
- **WHEN** `opp_contract_type(client, opp_id)` is evaluated
- **THEN** it returns `"standard"` — the normalized match is equality, not a
  prefix test on `CONSULTING`

#### Scenario: no quote lines means standard
- **GIVEN** an opportunity with no quotes or no quote lines
- **WHEN** `opp_contract_type(client, opp_id)` is evaluated
- **THEN** it returns `"standard"`

### Requirement: Only fixed-price opportunities re-anchor to net ACV

ACV-consuming outputs (forecast, quota, account roll-ups) SHALL prefer, for
`fixed_price` opportunities, the net ACV (`ACV_Opportunity_USD__c` /
`sf_acv`) over the gross `Consulting_Total_USD__c` / `sf_consulting_acv`. For
`standard` opportunities — T&M and prepaid-credit consulting, where gross ≈ net
— the existing preference order (`consulting_acv → acv → arr`) SHALL remain
unchanged. All preference comparisons SHALL use is-not-None semantics so a
genuine `0.0` is honored.

#### Scenario: fixed-price prefers net over gross
- **GIVEN** a fixed-price opportunity with gross `consulting_acv = 200000.0`
  and net `acv = 100000.0`
- **WHEN** its effective ACV is computed for forecast, quota, or account
  roll-up
- **THEN** the effective ACV is `100000.0` (the net), correcting the ~2×
  overstatement

#### Scenario: standard contract types keep gross
- **GIVEN** a T&M or prepaid-credit opportunity classified `"standard"` with
  `consulting_acv = 150000.0` and `acv = 148000.0`
- **WHEN** its effective ACV is computed
- **THEN** the effective ACV is `150000.0` — the pre-change preference order is
  byte-for-byte unchanged for non-fixed-price opportunities

#### Scenario: absent contract type behaves as standard
- **GIVEN** a pursuit file whose frontmatter has no `sf_contract_type` key
  (not yet re-synced)
- **WHEN** forecast or quota computes its effective ACV
- **THEN** it is treated as `"standard"` and the existing preference applies —
  existing files are unaffected until re-sync

### Requirement: Fixed-price with no live net ACV degrades to gross, not zero

A `fixed_price` opportunity whose net ACV is absent SHALL fall back to the
gross `consulting_acv` — never to zero and never past gross — whenever
`ACV_Opportunity_USD__c` / `sf_acv` is None, including a degraded live fetch
when a live net value is unavailable.
Gross is a bounded overstatement (today's status quo); zero silently drops the
deal from every roll-up. A genuinely live `0.0` net ACV is not absence and
SHALL be honored as `0.0` under is-not-None semantics.

#### Scenario: missing net ACV falls back to gross
- **GIVEN** a fixed-price opportunity with `acv = None` and
  `consulting_acv = 200000.0`
- **WHEN** its effective ACV is computed (e.g.
  `select_effective_acv("fixed_price", net=None, gross=200000.0, arr=None)`)
- **THEN** the result is `200000.0` — the gross value, not `0.0` and not None

#### Scenario: genuine zero net ACV is honored
- **GIVEN** a fixed-price opportunity with a live `acv = 0.0` and
  `consulting_acv = 200000.0`
- **WHEN** its effective ACV is computed
- **THEN** the result is `0.0` — zero is a value, not an absence

#### Scenario: per-opp walk failure degrades to status quo
- **GIVEN** `sf account` rendering an open opportunity whose quote-line walk
  fails with a non-auth error
- **WHEN** the account view computes that opp's contract type and effective ACV
- **THEN** the opp is treated as `"standard"` (gross preference — today's
  behavior), the failure is logged, the render completes, and an `SFAuthError`
  would instead propagate to `cli_main()` (exit 2)

### Requirement: The re-anchor decision has a single domain home

The ACV re-anchor policy SHALL exist exactly once — which fields, in which
order, per contract type, including the gross-not-zero degrade — as a pure
shape-agnostic helper in `fieldkit.sf.components`
(`select_effective_acv(contract_type, net, gross, arr)`), and all consuming
adapters (`commands/sf/account.py`, `commands/pursuit/forecast.py`,
`commands/pipeline/quota.py`) SHALL call it, performing only their own field
mapping (Constitution Principle I). No `fixed_price`
preference conditional SHALL exist outside `sf/components.py`. Likewise,
`FAMILY_BUCKETS` and `FIXED_PRICE_FAMILY` are defined once in
`fieldkit.sf.components` and imported everywhere else; related component services extend — rather than redefine — this module.

#### Scenario: all three adapters consume the one helper
- **GIVEN** the three ACV consumers with their three data shapes (live opp
  dict, raw frontmatter dict, `PursuitFrontmatter` model)
- **WHEN** each computes an effective ACV for the same fixed-price opportunity
- **THEN** each maps its fields (`acv`/`sf_acv`, `consulting_acv`/
  `sf_consulting_acv`, `arr`/`sf_arr`) and delegates the decision to
  `select_effective_acv`, so all three report the same figure

#### Scenario: a policy change is a one-site edit
- **GIVEN** a future change to the preference order (e.g. a third contract
  type)
- **WHEN** the policy is updated in `sf/components.py`
- **THEN** forecast, quota, and account all reflect the new order with no
  lockstep edits in `commands/`, and a grep for a `fixed_price` preference
  conditional outside `sf/components.py` finds nothing

### Requirement: Deterministic, provenance-carrying output

Contract type SHALL be computed at sync time and persisted as
`sf_contract_type` in pursuit frontmatter alongside the other `sf_*`
provenance fields (written only via `write_frontmatter_raw()`), and emitted in
the `sf opportunity --json` payload. All ACV figures SHALL be sourced from
Salesforce record fields (`ACV_Opportunity_USD__c`, `Consulting_Total_USD__c`)
— never estimated or interpolated — and repeated runs against the same
records SHALL produce identical output.

#### Scenario: sync persists the classification
- **GIVEN** a pursuit synced via `sf frontmatter` for a fixed-price
  opportunity
- **WHEN** the sync completes
- **THEN** the frontmatter contains `sf_contract_type: fixed_price`, and
  subsequent forecast/quota runs read the persisted key without touching live
  Salesforce

#### Scenario: repeated runs are stable
- **GIVEN** unchanged Salesforce records and unchanged pursuit frontmatter
- **WHEN** forecast or quota is run twice
- **THEN** both runs emit identical ACV figures, each traceable to a persisted
  `sf_*` field sourced from a Salesforce record
