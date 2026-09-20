# shadowbot-auth Specification

## Purpose
Define the current behavioral contract for shadowbot-auth, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Chrome cookie token acquisition
The auth module SHALL extract a valid Keycloak access token from the user's live
Chrome browser session without requiring manual user action, provided the configured Chrome profile (default: Default)
is authenticated to the configured authorization host.

**Security constraint**: Exception messages raised by this module MUST NOT include raw
cookie values, decrypted bytes, AES key material, auth codes, or token strings. Exception
messages SHALL describe what failed (e.g., "PKCS7 unpadding failed") without embedding
sensitive byte values. This applies to all `ShadowbotAuthError` instances raised
throughout this module.

**Chrome Cookies path validation** (for `_decrypt_chrome_cookies()`, separate from state dir):
1. Check `cookie_path.is_symlink()` on the **unresolved** path — if `True`, raise `ShadowbotAuthError`
   (symlinks to regular files pass `is_file()` but are path-traversal risks)
2. Check `cookie_path.is_file()` — if `False`, raise `ShadowbotAuthError`

**State directory path validation** (for `get_state_dir()`):
1. Check `Path(config_value).is_symlink()` on the **unresolved** path — if `True`, raise `ShadowbotAuthError`
   (MUST be before calling `.resolve()`; post-resolve `.is_symlink()` always returns False)
2. Call `.resolve()` on the path
3. Verify the resolved path is within `get_data_root()` or `~/.config/fieldkit/`
All checks required; paths failing any check MUST raise `ShadowbotAuthError`.

Acquisition process:
1. Read Chrome's encrypted cookie DB from the configured path (default:
   `~/.config/google-chrome/Default/Cookies`). If the path does not exist, raise
   `ShadowbotAuthError` with message directing user to set `shadowbot_chrome_cookies_path`.
   Open source DB in read-only URI mode: `sqlite3.connect(f"file:{path}?mode=ro", uri=True,
   timeout=5)`. Then use `sqlite3.Connection.backup()` to copy into an in-memory connection.
   Read-only mode + SQLite's backup API handles WAL mode transparently; no sidecar copy
   needed; 5-second timeout allows busy-retry if Chrome holds a transient write lock.
   Close source connection after backup. Query the in-memory copy.
2. Retrieve Chrome Safe Storage key from GNOME keyring via `secretstorage`.
3. Derive 16-byte AES key: PBKDF2-SHA1, salt=`b'saltysalt'`, iterations=1.
   (Linux Chrome GNOME keyring: `os_crypt_linux.cc` uses `kSalt = "saltysalt"`, `kEncryptionIterations = 1`.
   macOS uses `salt=b'peanuts'` + 1003 iterations — different platform, not supported here.)
4. Decrypt `AUTH_SESSION_ID` and `KEYCLOAK_SESSION` for the configured authorization host:
   strip 3-byte `v11` prefix; AES-128-CBC (IV=`b' '*16`); PKCS7-unpad;
   strip 32-byte domain hash. Raise `ShadowbotAuthError` if prefix is not `v11`.
5. Perform silent OIDC request (`prompt=none`) to auth endpoint with `follow_redirects=False`.
6. Validate 302 response and extract `code` from `Location` header.
7. Exchange code for tokens via POST to token endpoint with body:
   `grant_type=authorization_code`, `client_id=shadowbot-ui`, `code=<code>`,
   `redirect_uri=<configured redirect URI>`
   (MUST exactly match the redirect_uri used in step 5).
8. Persist tokens and clear in-memory cache. If the server does not return a
   new `refresh_token`, retain the existing refresh token from the token file
   (Keycloak may or may not rotate refresh tokens depending on configuration).

#### Scenario: Successful silent acquisition
- **WHEN** the configured Chrome profile (default: Default) has a live `KEYCLOAK_SESSION` for the configured authorization host
- **THEN** `acquire_from_chrome()` returns without error
- **THEN** access token and refresh token are written to the token file
- **THEN** the in-memory cache is cleared

#### Scenario: Chrome session expired (login_required)
- **WHEN** the 302 Location contains `?error=login_required`
- **THEN** `ShadowbotAuthError` is raised directing the user to log into the configured service in Chrome and ensuring the Default profile (or configured path) is active

#### Scenario: Non-302 response from auth endpoint
- **WHEN** the auth endpoint returns any status other than 302
- **THEN** `ShadowbotAuthError` is raised stating the unexpected status

#### Scenario: 302 with no Location header
- **WHEN** the auth endpoint returns 302 but no `Location` header
- **THEN** `ShadowbotAuthError` is raised

#### Scenario: secretstorage unavailable
- **WHEN** `secretstorage` cannot be imported
- **THEN** `_HAS_CHROME_AUTH` is `False`
- **THEN** `acquire_from_chrome()` raises `ShadowbotAuthError` with message suggesting `--refresh-token`

#### Scenario: GNOME keyring locked or dbus unavailable
- **WHEN** `secretstorage` is importable but the GNOME keyring default collection
  is locked OR the dbus session bus is unavailable (e.g., headless SSH terminal
  without `$DBUS_SESSION_BUS_ADDRESS`)
- **THEN** `ShadowbotAuthError` is raised describing the keyring/dbus issue
- **THEN** the error message suggests `fieldkit auth shadowbot --refresh-token`

#### Scenario: Chrome Cookies file not found
- **WHEN** the configured Chrome Cookies path does not exist
- **THEN** `ShadowbotAuthError` is raised naming the missing path

#### Scenario: Unsupported cookie encryption version
- **WHEN** a cookie value has a prefix other than `v11` (e.g., `v20`)
- **THEN** `ShadowbotAuthError` is raised with message "unsupported Chrome cookie version: v20"

#### Scenario: acquire_from_chrome end-to-end success
- **WHEN** all prerequisites are satisfied
- **THEN** token file is written and cache is cleared

#### Scenario: acquire_from_chrome decrypt step fails
- **WHEN** cookie decryption raises any exception
- **THEN** `ShadowbotAuthError` is raised describing what failed (e.g., "cookie decryption failed: PKCS7 unpadding error")
- **THEN** the error message MUST NOT contain raw bytes, key material, or cookie values

#### Scenario: Chrome Cookies path is not a regular file
- **WHEN** the configured `shadowbot_chrome_cookies_path` path points to a symlink, directory, or FIFO
- **THEN** `ShadowbotAuthError` is raised with the path and type of the invalid file

#### Scenario: Decryption happy path (contract test)
- **WHEN** `_decrypt_chrome_cookies()` receives a pre-computed AES-128-CBC encrypted cookie blob (with v11 prefix, PKCS7 padding, 32-byte domain hash prefix)
- **THEN** the returned dict contains the expected plaintext values
- **NOTE** This MUST be tested with a hardcoded fixture (no live Chrome or keyring needed)

### Requirement: Refresh token fallback
The auth module SHALL accept a manually-injected Keycloak refresh token, validate
it, and persist both resulting tokens to the token file.

#### Scenario: Valid refresh token injected
- **WHEN** `inject_refresh_token(rt)` is called with a valid token
- **THEN** new tokens are exchanged via `POST /token`
- **THEN** both written to token file with `0o600` permissions
- **THEN** in-memory cache cleared

#### Scenario: Invalid refresh token
- **WHEN** `inject_refresh_token(rt)` is called with an expired/invalid token
- **THEN** `ShadowbotAuthError` raised with HTTP status and error detail

### Requirement: Token cache
The auth module SHALL maintain an in-memory token cache with TTL of 240 seconds.

#### Scenario: Cache hit
- **WHEN** `get_token()` called within 240 s of prior success
- **THEN** cached token returned without network call

#### Scenario: Cache miss — refresh token valid
- **WHEN** cache is stale; token file has valid refresh token
- **THEN** `get_token()` exchanges for new access token
- **THEN** the returned string equals the new `access_token` (not the refresh token)
- **THEN** `_cache.is_valid()` returns `True` after the call
- **THEN** the token file is updated with the new `access_token` and `refresh_token`

#### Scenario: Cache miss — refresh expired, Chrome available
- **WHEN** cache is stale; `invalid_grant` from refresh; `_HAS_CHROME_AUTH=True`; session active
- **THEN** `acquire_from_chrome()` called; new token cached and returned
- **THEN** the returned string equals the new `access_token`

#### Scenario: All auth paths exhausted
- **WHEN** cache stale; refresh expired; Chrome also fails
- **THEN** `ShadowbotAuthError` raised with message containing `--refresh-token`

### Requirement: Token file security
Token file SHALL be written atomically using a write-to-tempfile-then-`os.replace()` pattern:
1. Create a tempfile in the same directory with `tempfile.mkstemp(dir=state_dir, prefix='.shadowbot-tmp-')` → `os.fchmod(fd, 0o600)`
2. Write the JSON content to the tempfile
3. Call `os.replace(tempfile_path, token_file_path)` — atomic on POSIX, no race window

This guarantees: (a) the file is never world-readable at any point, (b) the swap is
atomic (concurrent readers see either old or new content, never partial), (c) the
`os.fchmod` before `os.replace` ensures the final file has `0o600` regardless of the
destination's prior permissions.

File SHALL contain `access_token`, `refresh_token`, `token_uri`, `client_id`, `captured_at`.

#### Scenario: Token file written with correct permissions on first creation
- **WHEN** the token file does not exist
- **THEN** file created with mode `0o600`

#### Scenario: Token file permissions enforced on overwrite
- **WHEN** the token file already exists with broader permissions (e.g., `0o644`)
- **THEN** file is overwritten AND permissions explicitly set to `0o600` via `os.fchmod`

### Requirement: HTTP timeouts for auth requests
All HTTP requests in `auth.py` MUST use a timeout of 30 seconds: the silent OIDC GET, the
token exchange POST, and the refresh token POST. If the request times out,
`ShadowbotAuthError` is raised.

#### Scenario: Auth endpoint timeout
- **WHEN** the OIDC auth endpoint or token endpoint does not respond within 30 seconds
- **THEN** `ShadowbotAuthError` is raised indicating the timeout

### Requirement: Shared state directory resolution
A single `get_state_dir() -> Path` helper MUST resolve the state directory (renamed from `_get_state_dir` — no leading
underscore since it is an intentional cross-module export; defined in `auth.py`, imported
by `client.py`) resolves the state directory: parent of `shadowbot_token` config value if
set, else `<data-root>/data/`. Both `shadowbot-token.json` and `shadowbot-state.json`
live here.

`get_state_dir()` MUST validate in ORDER: (1) check pre-resolution `.is_symlink()`; (2) call
`.resolve()`; (3) verify resolved path is within `get_data_root()` or `~/.config/fieldkit/`.
The `.resolve()` call MUST come AFTER the symlink check — post-resolve `.is_symlink()` is always
False (resolver follows links). If any check fails, `ShadowbotAuthError` MUST be raised.

#### Scenario: Config key present
- **WHEN** `shadowbot_token` is set in config to a path within `get_data_root()`
- **THEN** `get_state_dir()` returns its parent directory

#### Scenario: Config key absent
- **WHEN** `shadowbot_token` not set
- **THEN** `get_state_dir()` returns `<data-root>/data/`

#### Scenario: Config key resolves outside approved roots
- **WHEN** `shadowbot_token` resolves to a path outside `get_data_root()` and `~/.config/fieldkit/`
- **THEN** `ShadowbotAuthError` is raised

#### Scenario: Config key parent is a symlink (pre-resolution check)
- **WHEN** `Path(shadowbot_token).parent.is_symlink()` returns `True` (checked BEFORE `.resolve()`)
- **THEN** `ShadowbotAuthError` is raised (symlinks bypass prefix containment checks)
