# backstory-native-oauth Specification

## Purpose
Define the current behavioral contract for backstory-native-oauth, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Backstory authentication uses the gateway's native OAuth flow

fieldkit SHALL provide `fieldkit auth backstory`, which MUST delegate Backstory streamable-HTTP OAuth registration to MCPJungle without implementing or persisting OAuth credentials itself.

#### Scenario: Operator authorizes Backstory
- **GIVEN** MCPJungle with upstream OAuth support is installed and reachable
- **WHEN** the operator runs `fieldkit auth backstory` and completes browser consent
- **THEN** fieldkit registers the canonical Backstory endpoint through MCPJungle
- **AND** MCPJungle owns dynamic client registration, PKCE, callback handling, token storage, and refresh
- **AND** fieldkit reports authentication success

#### Scenario: Existing static registration is migrated
- **GIVEN** a canonical Backstory registration already exists with a static bearer token
- **WHEN** the operator runs `fieldkit auth backstory`
- **THEN** fieldkit explicitly requests replacement of only that canonical registration
- **AND** the replacement uses MCPJungle's native OAuth flow

### Requirement: Authentication invocation does not expose credentials

The Backstory auth adapter MUST NOT accept, store, log, or place access tokens, refresh tokens, authorization codes, client secrets, or bearer-token flags in its subprocess argument vector.

#### Scenario: Subprocess command is constructed
- **WHEN** fieldkit launches MCPJungle authentication
- **THEN** the argument vector contains only the registry URL, registration operation, canonical server metadata, endpoint URL, and force option
- **AND** stdin, stdout, and stderr remain inherited from the invoking process for the interactive flow
- **AND** fieldkit requires only stdin to be attached to a terminal, allowing stdout and stderr to be redirected
- **AND** no fieldkit credential file is created

#### Scenario: Noninteractive invocation is rejected before replacement
- **GIVEN** stdin is not attached to an interactive terminal
- **WHEN** the operator invokes `fieldkit auth backstory`
- **THEN** fieldkit reports an actionable authentication error before launching MCPJungle
- **AND** the existing Backstory registration is not mutated

### Requirement: Backstory gateway resolution fails closed on invalid config

Backstory authentication MUST use the canonical MCP gateway URL resolution path in strict mode before constructing the MCPJungle command. Strict resolution MUST perform a fresh, uncached read. A truly absent fieldkit config MUST retain the canonical environment-then-default fallback, while a dangling config symlink, non-directory path component, invalid encoding, unreadable file, malformed or non-mapping YAML, or schema-invalid config MUST raise `ConfigError` and MUST NOT launch MCPJungle.

#### Scenario: Existing fieldkit config is invalid
- **GIVEN** an existing fieldkit config cannot be read as a valid schema-conforming YAML mapping
- **WHEN** an interactive operator invokes `fieldkit auth backstory`
- **THEN** fieldkit reports the configuration failure through canonical exit code 3
- **AND** it does not construct or launch the registration subprocess
- **AND** it does not convert the failure to an authentication error

#### Scenario: Cached configuration becomes invalid
- **GIVEN** the process previously cached a valid fieldkit config
- **AND** the config on disk becomes invalid before Backstory reauthorization
- **WHEN** an interactive operator invokes `fieldkit auth backstory`
- **THEN** strict resolution reports the current configuration failure
- **AND** it does not launch the registration subprocess with the stale gateway URL

#### Scenario: fieldkit config is absent
- **GIVEN** the fieldkit config file does not exist
- **WHEN** an interactive operator invokes `fieldkit auth backstory`
- **THEN** the canonical gateway resolver uses the configured environment override when present
- **AND** otherwise uses its documented default endpoint
- **AND** fieldkit may proceed to construct the registration subprocess

### Requirement: Backstory authentication has bounded canonical failures

fieldkit MUST bound the interactive MCPJungle process with a named 900-second timeout and MUST map unavailable tooling, timeout, cancellation, or unsuccessful registration to the shared authentication exit code 2. The ceiling SHALL budget MCPJungle's 600-second upstream consent window plus 300 seconds for gateway bootstrap and completion; it MUST NOT be presented as a guarantee that OAuth will complete.

#### Scenario: MCPJungle is unavailable
- **GIVEN** the MCPJungle executable cannot be found
- **WHEN** the operator runs `fieldkit auth backstory`
- **THEN** fieldkit reports the missing dependency as an authentication error
- **AND** exits 2

#### Scenario: OAuth does not complete
- **GIVEN** the browser flow times out, is cancelled, or MCPJungle returns nonzero
- **WHEN** fieldkit handles the result
- **THEN** it reports a concise rerun instruction without reproducing credentials or captured OAuth output
- **AND** exits 2
- **AND** documentation warns that cancellation, browser failure, or timeout can leave the replaced registration unavailable until reauthorization succeeds

### Requirement: Refresh ownership is accurate in help and documentation

fieldkit SHALL state that MCPJungle owns normal Backstory token refresh and that rerunning `fieldkit auth backstory` starts reauthorization when repair is needed.

#### Scenario: Operator seeks a refresh mode
- **WHEN** the operator reads command help or integration documentation
- **THEN** the documentation does not advertise a fieldkit token injection or `--refresh` option
- **AND** directs the operator to rerun the auth command for reauthorization
