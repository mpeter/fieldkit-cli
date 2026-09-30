---
last_reviewed: 2026-09-29
covers:
  - pyproject.toml
  - src/fieldkit/__main__.py
  - src/fieldkit/commands/init/
  - src/fieldkit/commands/doctor/
  - src/fieldkit/config/
  - src/fieldkit/commands/skill/
  - src/fieldkit/skill/
  - src/fieldkit/skills/
audience: user
---

# Installation and first success

This guide proves the portable fieldkit core before you connect an external
service. It does not require Salesforce, Google, an AI provider, or access to an
organization-specific system.

## Prerequisites

- Python 3.11 or newer on a [supported platform](compatibility.md).
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/).

<!-- compatibility-policy:start -->
fieldkit's portable core is tested on CPython 3.11, CPython 3.12, CPython 3.13, and CPython 3.14 across `ubuntu-24.04` and `macos-15`. The same core contract runs with Fedora 43's system Python in a `fedora:43` container. CI separately installs the exact candidate with its `all` optional profile on CPython 3.11 for Ubuntu and macOS, then imports every advertised integration dependency. Native Windows is experimental. Linux-only integrations such as browser credential extraction and systemd units have narrower requirements than the core.
<!-- compatibility-policy:end -->

Confirm both commands are available:

```console
python3 --version
uv --version
```

## Install

Install the released base package:

```console
uv tool install fieldkit-cli
```

Confirm the installed package version:

```console
fieldkit --version
```

`fieldkit --version` reports the installed package version. To test a source
checkout, use the contributor path in
[CONTRIBUTING.md](https://github.com/mpeter/fieldkit-cli/blob/main/CONTRIBUTING.md)
and run `uv run fieldkit` from that checkout.

To install a profile instead of the base package, choose one of these commands:

```console
uv tool install 'fieldkit-cli[google]'      # Google APIs and OAuth
uv tool install 'fieldkit-cli[llm]'         # supported AI providers
uv tool install 'fieldkit-cli[web]'         # local web interface
uv tool install 'fieldkit-cli[chrome-auth]' # supported browser credential stores
uv tool install 'fieldkit-cli[all]'         # all packaged optional dependencies
```

Installing a profile does not configure a provider or authorize fieldkit to use
one. If fieldkit is already installed, add `--force` to replace its tool
environment with the selected profile. See [Integrations and
profiles](integrations.md) before enabling a service.

## Create your first workspace

Choose a directory whose contents fieldkit may create or update, then run:

```console
fieldkit init --minimal ./fieldkit-workspace
```

Minimal initialization is non-interactive. It creates generic workspace
structure without writing identity values, service credentials, or sample
customer records. It also writes or updates `~/.config/fieldkit/config.yaml`
(or `$XDG_CONFIG_HOME/fieldkit/config.yaml` when `XDG_CONFIG_HOME` is absolute),
making this directory the active workspace and setting its database paths.
Other existing configuration keys are preserved.

For a disposable trial that does not read file contents from or modify your normal
configuration, workspace, or runtime-data root, run the commands in a subshell:

```console
FIELDKIT_TRIAL_ROOT="$(mktemp -d)"
(
  export XDG_CONFIG_HOME="$FIELDKIT_TRIAL_ROOT/config"
  export FIELDKIT_DATA_DIR="$FIELDKIT_TRIAL_ROOT/runtime-data"
  export PYTHON_DOTENV_DISABLED=1
  unset GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET
  unset FIELDKIT_SKILLS_DIR
  fieldkit init --minimal "$FIELDKIT_TRIAL_ROOT/workspace"
  fieldkit doctor
  fieldkit skill list
)
echo "$FIELDKIT_TRIAL_ROOT"
```

The `echo` command prints the temporary directory containing the trial
configuration, workspace, and runtime data. The subshell restores your existing
environment when it exits. Verify the printed path before removing it.

If you already use fieldkit, choose the workspace you intend to make active or
back up that configuration file before trying a different workspace.

## Verify the installation

```console
fieldkit doctor
fieldkit skill list
```

`fieldkit doctor` exits successfully when the services it checks are healthy or
unconfigured and their checked settings are valid. It does not validate every
LLM, MCP, or driver setting. In the isolated trial above, Salesforce, Gmail,
Google, and ShadowBot are reported as disabled or not configured. If an
integration is enabled instead, fieldkit found existing configuration or
credentials. With no `FIELDKIT_SKILLS_DIR` or configured `fieldkit_root`
override, `fieldkit skill list` verifies packaged workflow resources can be
discovered. With the isolated roots and cleared credentials shown above,
neither command contacts an external provider.

If either command fails, keep the exact command, exit code, and sanitized error
text, then use [Troubleshooting](reference/troubleshooting.md) or the route in
[SUPPORT.md](https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md).

## Keep or replace the workspace

An initially empty, newly created workspace contains only the generic files
created by minimal initialization unless you add data. Existing workspace files
are preserved. You can keep it as your working directory. Before removing
a non-disposable workspace, initialize another workspace or update your fieldkit
configuration so it does not retain paths into a deleted directory.

Uninstalling the application removes the tool environment and launcher. It does
not remove workspaces, runtime data, or the active fieldkit configuration:

```console
uv tool uninstall fieldkit-cli
```

## Configure a real workspace

Run the interactive setup only when you are ready to supply identity, workspace,
and optional integration settings:

```console
fieldkit init
fieldkit doctor
```

Review [Workspace initialization](guides/init.md) first if you need unattended
setup. Treat its answers file as sensitive when it contains identity or OAuth
values.

## Next steps

- Learn the [main workflows](user-guide.md).
- Understand [where data lives and what can leave the machine](privacy.md).
- Configure only the [integration profiles](integrations.md) you need.
- Use the generated [CLI reference](cli-reference.md) for exact options.
