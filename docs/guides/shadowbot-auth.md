---
last_reviewed: 2026-09-13
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
It does not start a new browser login or extract a fresh Chrome session.

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

## Troubleshoot safely

```console
fieldkit doctor shadowbot
fieldkit auth shadowbot --help
```

An exit code of `2` means authentication needs user action. Request a current
refresh token or follow your administrator's Chrome recovery instructions. If
your organization does not operate the expected service or protocol, leave the
integration unconfigured.
