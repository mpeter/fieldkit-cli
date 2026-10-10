---
last_reviewed: 2026-10-10
covers:
  - src/fieldkit/commands/shadowbot/
  - src/fieldkit/shadowbot/
audience: user
---

# Connect an organization-provided assistant

The `shadowbot` commands are an adapter for a separately operated assistant
service. The fieldkit project does not host that service, issue access, or claim
that it is available to the public. Ignore this guide unless an administrator
has given you an authorized endpoint, assistant identifier, and login path.

## Check existing authentication

If an administrator has already configured this integration, check the stored
credential:

```console
fieldkit auth shadowbot
fieldkit doctor shadowbot
```

Without `--refresh-token-file`, `fieldkit auth shadowbot` checks the existing token.
It never opens a browser login. If the stored refresh token is expired or revoked
and the `chrome-auth` dependencies are installed (they are also part of the `all`
profile), the check attempts [Chrome recovery](#enable-chrome-recovery-on-linux),
which reads your Chrome cookie store. So do `fieldkit doctor`, `fieldkit doctor
shadowbot`, and every other `shadowbot` command.

## Authenticate for the first time

Obtain a Keycloak refresh token through your organization's authorized login
and administrator guidance. In an interactive terminal, run `fieldkit auth
shadowbot` and paste the token when prompted. On a headless host, write it to
a regular owner-only file and run:

```console
install -m 600 /dev/null ./shadowbot-refresh-token
# Paste the refresh token into ./shadowbot-refresh-token, then:
fieldkit auth shadowbot --refresh-token-file ./shadowbot-refresh-token
fieldkit doctor shadowbot
```

fieldkit exchanges the refresh token for service tokens and stores them under
the configured runtime-data root. Diagnostic logs omit cookie and token values.

fieldkit rejects symlinks and group- or world-readable secret files. Never place
the token in an issue, source control, or a diagnostic attachment.

## Enable Chrome recovery on Linux

On a supported Linux desktop:

```console
uv tool install 'fieldkit-cli[chrome-auth]'
```

This installs local browser credential-store support. When the server rejects a
stored refresh token as expired or revoked (`invalid_grant`), fieldkit can
attempt recovery from an existing authorized Chrome session. Chrome recovery is
not a first-time login path: configure a refresh token first.

Recovery happens automatically whenever any `shadowbot` command, `fieldkit auth
shadowbot`, `fieldkit doctor`, or `fieldkit doctor shadowbot` needs a token and the
stored refresh token has been rejected. fieldkit decrypts the Chrome cookie database
through the desktop keyring, sends the session cookies for the login domain to the
configured authorization endpoint in a silent sign-in, and stores the new tokens. Recovery is available whenever the `chrome-auth`
dependencies are installed, including through the `all` profile. To prevent any
cookie read, install a profile that does not include them.

### Choose the Chrome profile

By default fieldkit reads the cookie database of Chrome's `Default` profile. If
your organization's login lives in another profile, set `chrome_cookies_path`
under the `shadowbot:` section of `config.yaml` to that profile's `Cookies` file
(for example, the one in the `Profile 2` directory).

The key must be nested under `shadowbot:`. A top-level
`shadowbot_chrome_cookies_path` or a misspelled key such as `chrome_cookie_path`
is ignored, so Chrome recovery would keep reading the `Default` profile.
`fieldkit doctor` and `fieldkit doctor shadowbot` print a `warning:` line for each
such key, and `doctor --json` lists them in a `warnings` array. Warnings do not
change the health result or the exit status. An unrecognized key is never printed, because arbitrary key text can carry
personal identifiers. A near miss is reported by the setting it resembles (for
example, `resembles 'chrome_cookies_path'; check its spelling`), and any other
key, including a non-text key, is reported only as an unrecognized key. Chrome recovery errors, including
decryption and database failures, name
the profile directory (`Chrome profile 'Profile 2' (configured)`) and whether it
came from `configured` or `default` settings, without printing the full path.
A configured file outside a Chrome-style profile directory (`Default`,
`Profile <N>`, `Guest Profile`, `System Profile`) is reported as `configured
Chrome cookie database` instead, so other directory names are never echoed.

## Troubleshoot safely

```console
fieldkit doctor shadowbot
fieldkit auth shadowbot --help
```

An exit code of `2` means authentication needs user action. Request a current
refresh token or follow your administrator's Chrome recovery instructions. If
your organization does not operate the expected service or protocol, leave the
integration unconfigured.
