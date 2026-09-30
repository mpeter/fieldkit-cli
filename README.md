# fieldkit

[![CI](https://github.com/mpeter/fieldkit-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/mpeter/fieldkit-cli/actions/workflows/ci.yml)
[![Artifact compatibility](https://github.com/mpeter/fieldkit-cli/actions/workflows/compatibility.yml/badge.svg)](https://github.com/mpeter/fieldkit-cli/actions/workflows/compatibility.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.14-blue.svg)](docs/compatibility.md)

fieldkit is a local-first command-line workspace for structured sales and account
work. It combines Markdown-based pursuits and tasks with optional Salesforce,
Google, AI, web, and organization-provided integrations. Your workspace remains
separate from the application and under your control.

A source version is not a published release. Publication is established only by
an immutable release record with a signed tag, GitHub release, and package
artifacts; see [Release history](docs/releases.md) to verify published versions.
Future product work is tracked in the [roadmap](ROADMAP.md).

## Quickstart: install and prove the portable core

You need a [supported Python version](docs/compatibility.md) and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/).

```console
uv tool install fieldkit-cli
```

After installation, create and verify a credential-free local workspace:

```console
fieldkit init --minimal ./fieldkit-workspace
fieldkit doctor
fieldkit skill list
```

`init --minimal` creates a generic local workspace and makes it active in your
fieldkit user configuration. With a fresh configuration and no integrations
enabled, `doctor` exits successfully while reporting each unconfigured
integration as disabled. With no `FIELDKIT_SKILLS_DIR` or configured
`fieldkit_root` override, `skill list` prints the workflows bundled with the
installed package. This first success needs no service credentials.

The command updates your fieldkit user configuration. If you already use
fieldkit, follow the isolated trial in [Installation and first
success](docs/getting-started.md) instead.

## What it can do

- Create and validate local pursuit workspaces.
- Audit, forecast, and advance structured opportunity notes.
- Run local watchers and assemble a morning brief.
- Add Salesforce and Gmail context when you configure those services.
- Enable AI-assisted, local web, browser-auth, or organization-provided
  capabilities independently.
- Expose stable text, Markdown, and JSON output for scripts and agents.

Start with [What fieldkit does](docs/user-guide.md), then choose integrations from
[Integrations and profiles](docs/integrations.md). Read
[Local data and privacy](docs/privacy.md) before using real account or customer
information.

## Choose a capability profile

The base package is intentionally small. You can select a profile at install
time instead:

```console
uv tool install 'fieldkit-cli[google]'
uv tool install 'fieldkit-cli[llm]'
uv tool install 'fieldkit-cli[web]'
uv tool install 'fieldkit-cli[chrome-auth]'
uv tool install 'fieldkit-cli[all]'
```

Installing a profile downloads its Python dependencies; it does not configure a
provider, grant access, or transmit your fieldkit workspace data. If the base
tool is already installed, add `--force` to replace its tool environment with
the selected profile. See [Integrations and profiles](docs/integrations.md)
before enabling a service.

## Contribute

fieldkit welcomes bug reports, documentation, tests, code, design discussion,
and issue triage. A contributor checkout has one supported setup path. Before
running `pr-check`, prepare a clean committed candidate and set `QUALITY_BASE`
to the reviewed full commit SHA as described in [CONTRIBUTING.md](CONTRIBUTING.md):

```console
git clone https://github.com/<your-user>/fieldkit-cli.git
cd fieldkit-cli
make bootstrap
make pr-check
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before starting. Project authority is
described in [GOVERNANCE.md](GOVERNANCE.md), support routes in
[SUPPORT.md](SUPPORT.md), and private vulnerability reporting in
[SECURITY.md](SECURITY.md).

Maintainers preparing a release should follow [RELEASING.md](RELEASING.md).

Dependency changes follow the [dependency security policy](docs/dependency-security.md), including
license review, locked-graph auditing, and narrow exception requirements.

## Compatibility and status

The portable core is tested on CPython 3.11 through 3.14 on Ubuntu and macOS,
with a Fedora/RHEL-family container smoke path. Native Windows is experimental.
Individual integrations can have narrower platform or access requirements; see
[Compatibility](docs/compatibility.md).

The public compatibility contract for 1.x and the distinction between supported
and internal interfaces are documented in [Product model](docs/concepts.md).

## License

fieldkit is licensed under the [Apache License 2.0](LICENSE).
