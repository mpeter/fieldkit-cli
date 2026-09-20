# cli-exit-taxonomy Specification

## Purpose
Define the current behavioral contract for cli-exit-taxonomy, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Configuration failures use the data-error exit

The CLI exception boundary SHALL map every `ConfigError` to exit 3 and emit a concise configuration diagnostic without a traceback.

#### Scenario: malformed local configuration reaches a wrapped command

- **GIVEN** a command body wrapped by `cli_main()` raises `ConfigError`
- **WHEN** the exception reaches the boundary
- **THEN** the process exits 3
- **AND** stderr identifies a configuration error requiring investigation

#### Scenario: missing configuration reaches the dispatcher backstop

- **GIVEN** an unwrapped command raises `ConfigError` because required configuration is absent
- **WHEN** the dispatcher backstop handles the exception
- **THEN** it returns exit 3 through the shared exception mapper

### Requirement: Authentication failures remain distinct

Credential and authentication failures SHALL continue to map to exit 2 through typed authentication exceptions, independently of the configuration mapping.

#### Scenario: domain credential is expired

- **GIVEN** a command raises an `AuthError` subclass
- **WHEN** the exception reaches either CLI boundary
- **THEN** the process exits 2

#### Scenario: LLM provider rejects credentials

- **GIVEN** a command raises `LLMError` with category `auth`
- **WHEN** the exception reaches either CLI boundary
- **THEN** the process exits 2

### Requirement: The public exit taxonomy remains bounded

The CLI SHALL retain the canonical exit codes 0 success, 1 partial or explicitly requested policy failure, 2 authentication, and 3 data/validation, without adding another configuration-specific or attention-specific code. Commands that successfully emit complete attention reports SHALL exit 0 by default and MAY expose an explicit strict option that converts specified findings to exit 1.

Previously: Exit 1 was described only as partial, while pursuit health commands used it implicitly for complete attention reports.

#### Scenario: Operator reads the exit-code reference
- **WHEN** the exception mapping and common command guidance are documented
- **THEN** configuration failures appear under exit 3
- **AND** exit 2 guidance directs only credential or authentication repair
- **AND** complete attention reports are documented as successful by default
- **AND** any exit-1 findings policy is explicitly opt-in
