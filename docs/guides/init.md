---
last_reviewed: 2026-09-27
covers:
  - src/fieldkit/commands/init/
audience: user
---

# Initialize a workspace

Use minimal initialization for a credential-free first workspace:

```console
fieldkit init --minimal ./fieldkit-workspace
fieldkit doctor
fieldkit skill list
```

The initialization command creates `accounts`, `config`, and `data` directories
in the selected workspace and writes a generic `config/accounts.yaml` account
index. It does not create unused briefing configuration files.

It also writes the active workspace and database paths to your fieldkit user
configuration. Existing unrelated configuration keys are preserved. Rerunning
minimal initialization creates missing scaffold files but does not replace an
existing account index. Unrelated operator files remain untouched.

If existing user configuration cannot be read as a YAML mapping, initialization
stops with exit status `3` instead of replacing it. Preserve the file and repair
the reported configuration problem before retrying; do not delete it to force
initialization to proceed.
The wizard also checks existing account and identity YAML before creating its
workspace artifacts. Updates preserve unrelated fields and use atomic YAML
replacement; a failure is not permission to discard the existing files.

Unconfigured integrations are expected in the doctor output. For a disposable
trial that does not change your normal fieldkit configuration, use the isolated
procedure in [Installation and first success](../getting-started.md).

## Configure identity and accounts interactively

Run `fieldkit init` when you are ready to store identity, account names, and
optional integration settings.

The wizard shows a summary before writing. It creates identity and account
configuration, one directory per configured account, and the fieldkit user
configuration. Existing account entries, account notes, and unrelated operator
files are preserved. Review the selected workspace
and every value in the summary before confirming.

After writing the workspace, the interactive wizard attempts to install bundled
skills into a detected agent tool. That step can update the tool's local project
configuration and does not have a separate confirmation prompt. Use unattended
setup if you need to defer it; unattended setup prints the install command
instead. Skill installation is separate from read-only discovery with `fieldkit
skill list`.

## Unattended setup

For a synthetic fixture or scripted onboarding, put the answers in a YAML file
and pass it to init:

```text
fieldkit init --answers init-answers.yaml
```

The required keys are `name`, `email`, and `data_dir`. This example also creates
one account workspace:

```yaml
name: Example User
email: user@example.com
data_dir: ./fieldkit-workspace
role: Account Executive
company: Example Company
territory: East
salesforce_user_id: ""
accounts:
  - Acme Corp
```

Optional keys are `role`, `company`, `territory`, `salesforce_user_id`,
`accounts`, `oauth_client_id`, `oauth_client_secret`, and
`shadowbot_assistant_id`. Omit optional keys you do not use. An OAuth client
secret requires a client ID. A relative data directory is resolved from the
directory where you run the command; use an absolute path when that context may
vary.

Account names may contain letters, digits, spaces, hyphens, and underscores;
fieldkit converts spaces to hyphens for the workspace directory name.

The answers document is validated before any configuration is written. Unknown
keys, missing required values, incorrect types, an unsafe account name, an OAuth
secret without a client ID, or an invalid ShadowBot assistant ID cause exit code
3 and leave the workspace unchanged. A valid answers file skips stdin, the
confirmation prompt, and interactive agent-harness discovery. It writes the
same workspace and configuration artifacts as the interactive wizard, then
prints the remaining post-setup guidance.

An answers file containing identity or OAuth values is sensitive. Keep it
outside the fieldkit source repository, restrict its permissions, and remove it
when it is no longer needed.
Use a regular UTF-8 YAML file no larger than 1 MiB. Initialization rejects
symlinked, unstable, unreadable, or oversized answers files without including
their path or contents in parse-error diagnostics.
Duplicate mapping keys are rejected rather than silently choosing the last
value. Unknown-key and invalid-account diagnostics do not echo the supplied
values.

Optional OAuth values are written to a private, mode-0600 `.env` file using
dotenv syntax. This file is data for fieldkit's loader, not a shell script; do
not source it. Surrounding whitespace is trimmed from answer values. Literal
dollar signs, quotes, backslashes, Unicode, and remaining internal LF newlines
are preserved without variable interpolation. Remaining NUL and carriage-return
characters are rejected before workspace artifacts are created.

## Verify what changed

Initialization can update both the selected workspace and your fieldkit user
configuration. After any mode completes, run `fieldkit doctor`, inspect the
workspace before adding real customer data, and read [Local data and
privacy](../privacy.md). Installing an optional dependency profile or running
initialization does not authorize an external service; each integration still
requires its own configuration and authentication.
The wizard records a discovered application root only when running from an
identified fieldkit source tree. An installed package does not add a guessed
checkout path; existing explicit overrides are preserved. With no
`FIELDKIT_SKILLS_DIR` or configured `fieldkit_root` override, workflows are
discovered through package resources.

Initialization rejects malformed existing configuration rather than replacing
it. Both modes re-read global configuration before merging; account and identity
updates retain unknown fields. Before creating artifacts, initialization rejects
pre-existing child destinations that redirect through symlinks or have
incompatible file types. The selected workspace itself may be an alias to an
existing directory, but not a dangling symlink. Keep its
directory tree stable while initialization runs: these checks do not guard
against concurrent renames or roll back earlier artifacts after a later failure.
