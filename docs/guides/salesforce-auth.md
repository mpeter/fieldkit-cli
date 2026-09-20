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
   [auth-sf] sid written to ~/.config/fieldkit/sf-cookies.json
   [auth-sf] Validating session against Salesforce API...
   [auth-sf] Session valid — authenticated
   ```

## Verify your session

```
fieldkit sf session-check
```

Expected output when the session is valid:

```
Session: ACTIVE
instance: yourorg.my.salesforce.com
```

Exit code 0 means fieldkit can reach Salesforce. Exit code 2 means the session
needs to be refreshed.

## Session lifespan

Your Salesforce organization controls session lifetime and invalidation. Check
the session before work that depends on Salesforce, and refresh the cookie when
`fieldkit sf session-check` exits `2`.

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

fieldkit rejects symlinks and group- or world-readable secret files.

## Signs something is wrong

**Session expired:**

```
Session: EXPIRED
SF session expired — run 'fieldkit auth sf' — copy the 'sid' cookie from browser DevTools at yourorg.my.salesforce.com
```

This is the most common error. Follow the refresh steps above.

**Session redirected to login:**

```
Session: EXPIRED
SF session expired (redirect to login) — run 'fieldkit auth sf' — copy the 'sid' cookie from browser DevTools at yourorg.my.salesforce.com
```

Your Chrome session has expired. Log back into `yourorg.my.salesforce.com` in
Chrome, then re-inject the sid.

**Suspicious sid format:**

```
[auth-sf] WARNING: sid does not look like a valid Salesforce session ID (expected format: 00DXXXXXXXXXXXXXXX!AQEA...). Proceeding anyway.
```

You may have copied the wrong cookie or included extra whitespace. Return to
DevTools, click the `sid` row again, and copy only the Value field. The sid must
contain a `!` character.

## What NOT to do

- **Do not use undocumented login helpers** — they are not part of fieldkit and may
  violate your organization's SSO configuration.
- **Do not set `SALESFORCE_*` environment variables** — fieldkit does not read them.
  All Salesforce auth goes through the cookie injected by `fieldkit auth sf`.
- **Do not copy the sid from the Lightning URL** — always use the
  `yourorg.my.salesforce.com` domain in DevTools, not `yourorg.lightning.force.com`.
  The Lightning sid does not work for REST API calls.
