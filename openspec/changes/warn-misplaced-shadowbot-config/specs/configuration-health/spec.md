## ADDED Requirements

### Requirement: Misplaced ShadowBot configuration keys are reported

Configuration diagnostics MUST warn about each key inside the `shadowbot:` section that the
ShadowBot configuration schema does not define, and about each top-level key whose name begins
with `shadowbot_`. When the unrecognized key, without its `shadowbot_` prefix, matches a defined
ShadowBot key, the warning MUST name the expected location (for example
`shadowbot.chrome_cookies_path`). Warnings MUST NOT change health status or exit status, and
unrecognized keys MUST remain valid configuration.

#### Scenario: Cookie path configured at the top level
- **GIVEN** a configuration with top-level `shadowbot_chrome_cookies_path` and no `shadowbot.chrome_cookies_path`
- **WHEN** the operator runs `fieldkit doctor shadowbot --json`
- **THEN** the result SHALL include a warning stating that the key is ignored and that `shadowbot.chrome_cookies_path` is expected
- **AND** the exit status SHALL be the same as without the misplaced key

#### Scenario: Misspelled key inside the section
- **GIVEN** a `shadowbot:` section containing `chrome_cookie_path`
- **WHEN** the operator runs `fieldkit doctor`
- **THEN** the ShadowBot entry SHALL include a warning naming `shadowbot.chrome_cookie_path` as unrecognized

#### Scenario: Correct configuration
- **GIVEN** a `shadowbot:` section containing only keys the schema defines
- **WHEN** the operator runs `fieldkit doctor shadowbot --json`
- **THEN** the `warnings` array SHALL be empty
