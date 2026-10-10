## Why

Issue #95: `get_shadowbot_chrome_cookies_path()` reads only
`shadowbot.chrome_cookies_path`. A top-level `shadowbot_chrome_cookies_path`
key, or a misspelled key inside the `shadowbot:` section, is ignored without
any message, so the Chrome fallback reads the `Default` profile. When the
identity-provider session lives in another profile, every refresh-token expiry
ends in `login_required`.

Refresh tokens are session-bound (about ten hours) even with `offline_access`,
so the Chrome fallback is the normal recovery path and a silent
misconfiguration fails every day.

The diagnostics don't help:

- The `login_required` error says "Default profile or configured path"
  without saying which one was read.
- The missing-file error embeds the absolute cookie path, which includes the
  home directory. The repository contract keeps absolute home paths out of
  diagnostics.

## What Changes

1. A config-domain function reports unrecognized keys in the `shadowbot:`
   section and top-level keys prefixed `shadowbot_`, naming the expected key
   when one matches. Known keys come from the existing `_ShadowbotConfig`
   schema, the single source of truth.
2. `fieldkit doctor` and `fieldkit doctor shadowbot` show those warnings.
   ShadowBot auth logs them once at WARNING when it resolves the cookie path.
   Warnings never change health status or exit codes; unknown keys stay valid
   under the existing extension policy.
3. Chrome-fallback errors identify the cookie database by Chrome profile
   directory name (for example `Default` or `Profile 2`) and whether it came
   from configuration or the default, never by absolute path.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `configuration-health`: diagnostics warn about misplaced or unrecognized
  ShadowBot configuration keys.
- `shadowbot-auth`: Chrome-fallback errors name the profile that was read
  without disclosing absolute paths.

### Removed Capabilities
- None.

## Impact

- `src/fieldkit/config/_shadowbot.py`, `config/_schema.py` (read-only use),
  `commands/doctor/_result.py`, `commands/doctor/shadowbot.py`,
  `commands/doctor/cli.py`, `shadowbot/auth.py`.
- `doctor --json` gains a `warnings` array per service: additive.
- Requires a `changelog.d/` fragment.

## Constitution Alignment

Assessed against the Unbound Force org constitution.

### I. Autonomous Collaboration

**Assessment**: N/A

No change to shared artifacts beyond an additive diagnostics field.

### II. Composability First

**Assessment**: PASS

ShadowBot remains optional. The key check runs only when a `shadowbot`
section or `shadowbot_*` key is present, and imports nothing from the
integration package.

### III. Observable Quality

**Assessment**: PASS

Warnings are machine-readable in `doctor --json`, and error messages state
which profile was read, making the failure diagnosable from its message alone.

### IV. Testability

**Assessment**: PASS

The key check is a pure function of a configuration mapping. Message content
is tested with synthetic paths under `tmp_path`.
