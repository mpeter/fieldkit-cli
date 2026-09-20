# companion-llm-decision Specification

## Purpose
Define the current behavioral contract for companion-llm-decision, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Proposal decisions layer optional LLM judgment over a deterministic baseline

The system SHALL compute a deterministic action first and MAY refine it with one LLM call per selected item at propose or act tier. Read tier and disabled or unavailable LLM operation SHALL retain useful deterministic behavior.

#### Scenario: LLM-disabled proposal

- **GIVEN** `NO_LLM=1` and a routable attention item
- **WHEN** the companion runs at propose tier
- **THEN** it writes a deterministic proposal without making a provider call
- **AND** the proposal records deterministic provenance and an LLM-disabled fallback category

#### Scenario: Read tier avoids LLM cost

- **GIVEN** an attention item and working LLM credentials
- **WHEN** the companion runs at read tier
- **THEN** it performs only the deterministic read behavior
- **AND** it makes no synthesis call

### Requirement: External evidence is bounded and treated only as data

Every model prompt SHALL keep instructions in a static system message and SHALL cap and wrap attention and enrichment fields as untrusted user data before placing them in the user message.

#### Scenario: Evidence contains instruction-like text

- **GIVEN** an alert summary or enrichment result containing instructions or a closing user-data delimiter
- **WHEN** the decision prompt is built
- **THEN** the content remains inside escaped untrusted-data delimiters
- **AND** the prompt stays within the component's documented size cap

### Requirement: Model responses are structurally validated before publication

The system SHALL accept only the declared JSON decision shape. It SHALL preserve valid recommendation prose while discarding an invalid candidate command, and SHALL fall back deterministically when the response itself is invalid.

#### Scenario: Model returns an unsafe command

- **GIVEN** a valid recommendation with a command that lacks `--dry-run`, names an unregistered command family, or targets an identity absent from the evidence
- **WHEN** the response is validated
- **THEN** the recommendation remains reviewable
- **AND** the proposal contains no executable candidate command
- **AND** its provenance reports the invalid-command fallback category

#### Scenario: Model returns malformed output

- **GIVEN** prose, malformed JSON, extra keys, or an oversized response
- **WHEN** the response is parsed
- **THEN** the deterministic action is used
- **AND** the loop records a stable parse fallback without persisting raw model output

### Requirement: Proposal generation and action execution are separate

The system SHALL execute only read-only enrichment before writing a proposal. Propose tier SHALL NOT execute a candidate mutation. Act tier SHALL execute a candidate only after the existing tier and configured allowlist gate permits it.

#### Scenario: Proposal contains a previewable mutation

- **GIVEN** a validated LLM decision containing a previewable mutation
- **WHEN** the loop runs at propose tier
- **THEN** it writes the candidate and concrete recommendation to the outbox
- **AND** it does not execute the candidate or count a gate denial

#### Scenario: Act tier receives the same candidate

- **GIVEN** the same candidate at act tier
- **WHEN** it is absent from the configured allowlist
- **THEN** the existing gate denies it and no subprocess executes
- **AND** the denial remains journaled with the existing exit taxonomy

### Requirement: Decision degradation is observable without leaking prompt data

Loop results and proposal artifacts SHALL identify deterministic versus LLM decisions and stable fallback categories. They SHALL NOT persist raw prompts, raw invalid responses, provider payloads, or credential details.

#### Scenario: Provider call fails

- **GIVEN** a provider auth, rate-limit, timeout, or general failure
- **WHEN** proposal judgment falls back
- **THEN** a deterministic proposal remains available
- **AND** the loop increments its LLM fallback count
- **AND** durable proposal data contains only the stable fallback category
