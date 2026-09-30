---
last_reviewed: 2026-09-27
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
Configure the administrator-provided `shadowbot` URLs and identifiers in
`config.yaml` before authenticating; fieldkit does not provide working service
endpoints. See the [configuration reference](../reference/config-file.md#organization-provided-assistant)
for the required keys and example structure.

## Check existing authentication

If an administrator has already configured this integration, check the stored
credential:

```console
fieldkit auth shadowbot
fieldkit doctor shadowbot
```

Without `--refresh-token-file`, `fieldkit auth shadowbot` obtains a usable access
token from its in-memory cache or by refreshing the saved credential. Refreshing
can contact the service and update the saved tokens. If the service rejects the
refresh token as expired or revoked, configured Chrome recovery may run.

If authentication still fails, an interactive terminal prompts for a replacement
refresh token. A non-interactive invocation exits `2` with reauthorization
guidance instead of prompting. The command does not launch a browser login.

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

fieldkit exchanges the refresh token for service tokens. By default, it stores
them under the configured runtime-data root; an explicit `shadowbot_token` path
can place them under an approved workspace, runtime-data, or fieldkit configuration
directory. Diagnostic logs omit cookie and token values.

The file must contain non-empty UTF-8 text and be at most 8,192 bytes, including
surrounding whitespace. fieldkit rejects symlinks, pipes, directories, and
group- or world-accessible secret files before exchanging the token. Never
place the token in an issue, source control, or a diagnostic attachment.

## Enable Chrome recovery on Linux

On a supported Linux desktop:

```console
uv tool install --force 'fieldkit-cli[chrome-auth]'
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

If saving refreshed credentials fails, fieldkit exits `3` without reporting
authentication success. Check token storage permissions and available disk
space, then reauthenticate with `fieldkit auth shadowbot`. The service may have
rotated the refresh token, so the previously saved token may no longer work.
