# companion-loop Specification

## Purpose
Define the current behavioral contract for companion-loop, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: A headless single-pass loop drives the attention feed through the tier gate

The system MUST provide `fieldkit companion loop --once`, which performs one
poll→decide→gate→act→journal pass over the attention feed and exits, with no
dependency on any external agent harness (no `claude`, `opencode`, or `/loop`
invocation). It MUST decide each item's candidate action from a deterministic table
(no LLM required) and MUST honor the configured `companion.tier` via the existing
gate; it MUST NOT widen what a tier permits.

#### Scenario: a pass proposes for each new item at propose tier

- **GIVEN** `companion.tier` is `propose` and the feed yields two new attention items
- **WHEN** `fieldkit companion loop --once` runs
- **THEN** exactly two proposal files are written under `<fieldkit_data>/companion-outbox/`
- **AND** each proposal carries its source item's evidence path
- **AND** no state-mutating fieldkit command is executed

#### Scenario: the loop runs without any harness

- **GIVEN** neither `claude` nor `opencode` is installed on PATH
- **WHEN** `fieldkit companion loop --once` runs at `propose` tier
- **THEN** the pass completes and writes its proposals and journal records normally

### Requirement: The loop honors the read⊂propose⊂act tier ladder without widening it

At `read` tier the loop MUST NOT write outbox proposals and MUST NOT execute any
state-mutating command. At `propose` tier it MAY write outbox proposals but MUST
NOT execute state-mutating commands. At `act` tier the gate MAY additionally
execute only commands permitted by the exact-argv act allowlist. The current
deterministic `decide.propose_for()` layer emits only read-only commands, so it
MUST NOT be documented as producing allowlisted mutations. Production `act` and
`propose` therefore have the same proposal, mutation, and allowlist effects until
a mutating decision layer exists, while the result MUST continue to report the
effective configured tier.
A `--tier` override MUST only lower the effective tier; requesting a tier higher
than the configured one MUST fail closed.

#### Scenario: read tier writes no proposals

- **GIVEN** `companion.tier` is `read` and the feed yields a new item
- **WHEN** `fieldkit companion loop --once` runs
- **THEN** no file is written under `<fieldkit_data>/companion-outbox/`
- **AND** the item is still journaled so it is not re-handled

#### Scenario: the deterministic act tier does not execute mutations

- **GIVEN** `companion.tier` is `act`
- **WHEN** the deterministic decision layer produces an action
- **THEN** that action is read-only
- **AND** the act allowlist is not consulted
- **AND** proposal, mutation, and other side effects match `propose` tier
- **AND** the result still reports `tier` as `act`

#### Scenario: the act gate permits only an injected allowlisted command

- **GIVEN** a mutating action is supplied through the future decision seam
- **AND** the act allowlist contains exactly one matching `--dry-run`-capable entry
- **WHEN** the action reaches the act gate
- **THEN** the exact matching action is permitted
- **AND** a non-matching mutating action is denied and not executed

### Requirement: Every processed item is journaled and never re-handled

The loop MUST append one journal record per processed item (triage, proposal, or
executed action). A subsequent pass MUST NOT re-handle an item that already has a
journaled outcome.

#### Scenario: a proposed item is not re-proposed on the next pass

- **GIVEN** a pass at `propose` tier proposed for an item and journaled it
- **WHEN** `fieldkit companion loop --once` runs again with the same feed inputs
- **THEN** that item produces no new proposal file and no new journal record

### Requirement: The loop's outcome is observable and its failures are never silent

The loop MUST return a machine-parseable result carrying per-tier counts
(triaged, proposed, acted, denied) and MUST surface failures via exit code: a
malformed upstream feed input exits 3 (data), an authentication failure propagates
as exit 2, and a pass in which some best-effort enrichment reads failed is reported
as partial rather than silently succeeding or aborting.

#### Scenario: malformed feed input exits data-error, not silently

- **GIVEN** the watcher run-status JSON on disk is not valid JSON
- **WHEN** `fieldkit companion loop --once` runs
- **THEN** the command exits 3 (data error) and does not report success

#### Scenario: enrichment failure degrades, it does not abort

- **GIVEN** `companion.tier` is `propose` and a proposal's read-only enrichment command fails
- **WHEN** the loop processes that item
- **THEN** the proposal file is still written, noting enrichment was unavailable
- **AND** the failure is counted in the result rather than crashing the pass

### Requirement: Preserve unresolved attention items without fabricated action evidence

When a companion attention item resolves to no command, the loop MUST leave the
item eligible for subsequent passes at every tier and MUST NOT create an outbox
proposal, cooldown, or journal record claiming that an action completed.
The loop result MUST count the item as unresolved separately from proposed,
enriched, acted, denied, and failed command outcomes.

#### Scenario: Account-less read-tier item
- **GIVEN** an attention item with no account that resolves to no command
- **WHEN** the companion loop runs at the read tier
- **THEN** the item SHALL receive no cooldown, the journal SHALL contain no
  action record for that item, and the result SHALL count it as unresolved

#### Scenario: Account-less proposal-tier item
- **GIVEN** an attention item with no account that resolves to no command
- **WHEN** the companion loop runs at the propose or act tier
- **THEN** the item SHALL produce no outbox proposal or journal record and SHALL
  remain eligible for the next pass

#### Scenario: Routed command outcome remains journaled
- **GIVEN** an attention item that resolves to a command
- **WHEN** the companion loop runs that command or the gate denies it
- **THEN** the loop SHALL append the existing command or denial journal record

### Requirement: Successful actions create bounded suppression; proposals suppress while pending

Successful read-tier and act-tier work MUST put the item on a 24-hour cooldown.
A valid pending proposal MUST suppress its item while it remains in the outbox.
Explicit expiry MUST install the same 24-hour cooldown before removing the
proposal, after which a still-true condition may return.

#### Scenario: Expired pending proposal transitions to cooldown
- **GIVEN** a pending proposal currently retires an attention item
- **WHEN** the proposal is expired by the retention domain
- **THEN** the outbox no longer suppresses the item
- **AND** a 24-hour cooldown continues to suppress it
