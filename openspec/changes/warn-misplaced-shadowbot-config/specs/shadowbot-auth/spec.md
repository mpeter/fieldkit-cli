## ADDED Requirements

### Requirement: Chrome-fallback errors identify the profile read

Every `ShadowbotAuthError` raised while reading or using the Chrome cookie database MUST identify
the database by its Chrome profile directory name (the cookie file's parent directory name, for
example `Default` or `Profile 2`) and by its source, `configured` or `default`. These messages
MUST NOT include an absolute filesystem path. When misplaced ShadowBot configuration keys are
present, ShadowBot auth MUST log the configuration warning once at WARNING level before reading
the cookie database.

#### Scenario: Session expired in the default profile
- **GIVEN** no `shadowbot.chrome_cookies_path` and a Chrome session that returns `login_required`
- **WHEN** ShadowBot auth falls back to Chrome cookies
- **THEN** the error SHALL state that the `Default` profile from the `default` source was read
- **AND** it SHALL NOT contain the user's home directory

#### Scenario: Configured cookie file is missing
- **GIVEN** `shadowbot.chrome_cookies_path` pointing to a non-existent file in a profile directory named `Profile 2`
- **WHEN** ShadowBot auth attempts the Chrome fallback
- **THEN** the error SHALL name `Profile 2` and the `configured` source
- **AND** it SHALL NOT contain an absolute path

#### Scenario: Misplaced key present during auth
- **GIVEN** a top-level `shadowbot_chrome_cookies_path` key
- **WHEN** ShadowBot auth resolves the cookie path
- **THEN** a single WARNING log record SHALL name the expected key `shadowbot.chrome_cookies_path`
