---
last_reviewed: 2026-09-13
covers:
  - src/fieldkit/sf/client.py
  - src/fieldkit/commands/sf/
audience: ae-user
---

# Authenticate with Salesforce

## Why cookie auth

Some Salesforce organizations use SAML SSO and block programmatic username/password
login. fieldkit reads the session cookie that
your browser already holds after you log in through the normal SSO flow. You copy
that cookie once, paste it into fieldkit, and fieldkit uses it for all Salesforce
API calls until the session expires.

## Prerequisites

- Chrome is open and you are logged into `yourorg.my.salesforce.com`
- fieldkit is installed (`fieldkit --version` returns a version string)
- Configure the REST organization URL before installing the cookie: set
  `sf_org_url: https://yourorg.my.salesforce.com` in `config.yaml`, or
  `salesforce.org_url` in the workspace's `accounts.yaml`. See the
  [configuration reference](../reference/config-file.md#optional-integration-keys)
  for configuration locations. An absent or invalid organization URL is refused
  before the cookie is written.

## Initial setup

1. In Chrome, navigate to `https://yourorg.my.salesforce.com` and confirm you
   are logged in (your name appears in the top-right corner).

2. Open Chrome DevTools: press **F12** (or right-click anywhere → Inspect).

3. Click the **Application** tab in DevTools.

4. In the left sidebar, expand **Cookies** and click
   `https://yourorg.my.salesforce.com`.

5. In the cookie list, find the row where **Name** is `sid`. Click that row.

6. In the detail pane at the bottom, select the entire **Value** string and copy it
   (Ctrl+C). The value contains a `!` character (format: `00DXXXXXXXXXXXXXXX!AQEA...`).

7. Run the interactive command and paste the value when prompted. The input is
   hidden and never placed in process arguments:

   ```
   fieldkit auth sf
   ```

   Expected output (on stderr):

   ```
   [auth-sf] sid stored in fieldkit configuration
   [auth-sf] Validating session against Salesforce API...
   [auth-sf] Session valid — Salesforce API session is active.
   [auth-sf] Done.
   ```

   The validation messages omit the credential-file path and organization hostname.

## Verify your session

The cookie file must contain at most one matching REST session on
`my.salesforce.com` or a valid subdomain. Lookalike SID domains are skipped;
if one valid SID remains, the probe uses only that session. Duplicate JSON keys,
malformed entries, and ambiguous matching sessions are refused before the probe
sends credentials. Files must be regular UTF-8 files, not leaf symlinks or
special files, and are limited to 1,000,000 bytes and 1,000 cookie entries.
Re-run `fieldkit auth sf` to replace ambiguous or lookalike SID entries with one
session for the configured organization. Safely parsed non-SID cookies are kept;
malformed JSON is replaced with an explicit warning. Unsafe, oversized, or
non-UTF-8 files are not overwritten: repair or move the file aside first.
Both authentication input methods restore the prior credential bytes if session
validation rejects the candidate, raises an error, or is interrupted. If no
credential file existed, the failed candidate file is removed. This rollback
does not promise recovery from process termination or power loss.

```
fieldkit sf session-check
```

Expected output when the session is valid:

```
Session: ACTIVE
Salesforce API session is active.
```

Exit code `0` means the stored cookie successfully accessed the Account metadata
endpoint. The command currently uses exit `2` and the `EXPIRED` label for every
unsuccessful probe, including network failures and unexpected HTTP responses.
Read the accompanying message: a connectivity failure does not establish that
the credential expired.

## Session lifespan

Your Salesforce organization controls session lifetime and invalidation. Check
the session before work that depends on Salesforce. If `fieldkit sf session-check`
exits `2`, read its message. Refresh the cookie for a confirmed authentication
failure; for a network error, check connectivity and retry. For an unexpected
HTTP response, check Salesforce availability and permissions.

## Refresh your session

Follow the same steps as initial setup:

1. Confirm Chrome is logged into `yourorg.my.salesforce.com`.
2. Open DevTools → Application → Cookies → `https://yourorg.my.salesforce.com`.
3. Copy the `sid` cookie value.
4. Run `fieldkit auth sf` and paste the new value when prompted.

## Automation and headless hosts

For a non-interactive host, put the sid in a regular owner-only file. Do not
pass it on the command line or store it in an environment variable:

```console
install -m 600 /dev/null ./sf-sid
# Paste the sid into ./sf-sid, then:
fieldkit auth sf --sid-file ./sf-sid
```

The file must contain non-empty UTF-8 text and be at most 8,192 bytes, including
surrounding whitespace. fieldkit rejects symlinks, pipes, directories, and
group- or world-accessible secret files before installing credentials.

## Signs something is wrong

**Session expired:**

```
Session: EXPIRED
SF session expired — run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools
```

This is the most common error. Follow the refresh steps above.

**Session redirected to login:**

```
Session: EXPIRED
SF session expired (redirect to login) — run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools
```

Your Chrome session has expired. Log back into `yourorg.my.salesforce.com` in
Chrome, then re-inject the sid.

**Suspicious sid format:**

```
[auth-sf] WARNING: sid does not look like a valid Salesforce session ID (expected format: 00DXXXXXXXXXXXXXXX!AQEA...). Proceeding anyway.
```

You may have copied the wrong cookie or included extra whitespace. Return to
DevTools, click the `sid` row again, and copy only the Value field. Interactive
input is considered unusual unless it contains `!` and is longer than 20
characters. For SID format, the file-input path treats any nonempty value
containing `!` as usual; its file-security checks still apply.
These checks warn rather than prove validity; the API session check decides
whether authentication succeeds.

## What NOT to do

- **Do not use undocumented login helpers** — they are not part of fieldkit and may
  violate your organization's SSO configuration.
- **Do not set `SALESFORCE_*` environment variables** — fieldkit does not read them.
  API clients accept a non-empty `sf_session_id` configuration value before
  falling back to the cookie injected by `fieldkit auth sf`. The session-check
  command checks the cookie file, not that override; remove an obsolete override
  before relying on cookie authentication. Treat either credential as a secret.
- **Do not copy the sid from the Lightning URL** — always use the
  `yourorg.my.salesforce.com` domain in DevTools, not `yourorg.lightning.force.com`.
  The Lightning sid does not work for REST API calls.
