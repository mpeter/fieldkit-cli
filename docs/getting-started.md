---
last_reviewed: 2026-09-13
covers:
  - pyproject.toml
  - src/fieldkit/commands/init/
  - src/fieldkit/commands/doctor/
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

For the released package:

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

Optional profiles are independent:

```console
uv tool install 'fieldkit-cli[google]'      # Google APIs and OAuth
uv tool install 'fieldkit-cli[llm]'         # supported AI providers
uv tool install 'fieldkit-cli[web]'         # local web interface
uv tool install 'fieldkit-cli[chrome-auth]' # supported browser credential stores
uv tool install 'fieldkit-cli[all]'         # all packaged optional dependencies
```

Installing a profile does not configure a provider or authorize fieldkit to use
one. See [Integrations and profiles](integrations.md) before enabling a service.

## Create your first workspace

Choose a path outside the source repository, then run:

```console
fieldkit init --minimal ./fieldkit-workspace
```

Minimal initialization is non-interactive. It creates generic workspace
structure without writing identity values, service credentials, or sample
customer records. It also writes or updates `~/.config/fieldkit/config.yaml`,
making this directory the active workspace and setting its database paths.
Other existing configuration keys are preserved.

For an isolated trial that does not read or modify your normal fieldkit
configuration, set an absolute XDG configuration root for every trial command:

```console
FIELDKIT_TRIAL_ROOT="$(mktemp -d)"
export XDG_CONFIG_HOME="$FIELDKIT_TRIAL_ROOT/config"
fieldkit init --minimal ./fieldkit-workspace
fieldkit doctor
fieldkit skill list
echo "$FIELDKIT_TRIAL_ROOT"
unset XDG_CONFIG_HOME
```

The `echo` command prints the temporary directory containing the trial
configuration. `unset` restores the shell's normal configuration lookup for
later fieldkit commands. Verify the printed trial path before removing it.

If you already use fieldkit, choose the workspace you intend to make active or
back up that configuration file before trying a different workspace.

## Verify the installation

```console
fieldkit doctor
fieldkit skill list
```

`fieldkit doctor` exits successfully when the portable core is healthy and
reports unconfigured services as optional. `fieldkit skill list` verifies that
packaged resources can be discovered and rendered. Neither command contacts an
external provider in this minimal configuration.

If either command fails, keep the exact command, exit code, and sanitized error
text, then use [Troubleshooting](reference/troubleshooting.md) or the route in
[SUPPORT.md](https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md).

## Keep or replace the workspace

The workspace contains only the generic files created by minimal initialization
unless you add data. Keep it as your working directory. Before removing it,
initialize another workspace or restore your previous configuration so fieldkit
does not retain paths into a deleted directory. Uninstalling the application
does not remove workspaces or the active fieldkit configuration file:

```console
uv tool uninstall fieldkit-cli
```

## Configure a real workspace

Run the interactive setup when you are ready to supply identity, workspace, and
optional integration settings:

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
