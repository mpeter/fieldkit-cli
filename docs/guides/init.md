---
last_reviewed: 2026-09-10
covers:
  - src/fieldkit/commands/init/
audience: ae-user
---

# Workspace Initialization

Run the interactive wizard for first-time setup:

```text
fieldkit init
```

The wizard writes fieldkit's global configuration, identity and account
configuration, account directories, and these empty operator judgment files:

- `config/clocks.json`
- `config/engines.json`
- `config/people.json`
- `config/watchlist.json`

Rerunning init fills in a missing judgment file but never replaces an existing
one.

## Unattended setup

For CI, synthetic fixtures, or scripted onboarding, put the answers in a YAML
file and pass it to init:

```text
fieldkit init --answers init-answers.yaml
```

The required keys are `name`, `email`, and `data_dir`. This complete example
shows every accepted key:

```yaml
name: Example User
email: user@example.com
data_dir: /srv/fieldkit/workspace
role: Account Executive
company: Example Company
territory: East
salesforce_user_id: ""
accounts:
  - Acme Corp
gcp_project: example-project
oauth_client_id: ""
oauth_client_secret: ""
shadowbot_assistant_id: ""
```

Account names may contain letters, digits, spaces, hyphens, and underscores;
fieldkit converts spaces to hyphens for the workspace directory name.

The answers document is validated before any configuration is written. Unknown
keys, missing required values, incorrect types, an unsafe account name, an OAuth
secret without a client ID, or an invalid ShadowBot assistant ID cause exit code
3 and leave the workspace unchanged. A valid answers file skips stdin, the
confirmation prompt, and interactive agent-harness discovery. It writes the
same workspace and configuration artifacts as the interactive wizard, then
prints the remaining post-setup guidance.

An answers file containing OAuth credentials is sensitive. Restrict its file
permissions and remove it when it is no longer needed.
