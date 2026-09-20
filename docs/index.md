# fieldkit documentation

fieldkit is a local-first command-line workspace for account engineering. It brings structured
pipeline notes, Salesforce and Gmail context, meeting workflows, and optional AI assistance into
one programmable interface while keeping working data on your machine.

Start with [Installation and first success](getting-started.md). The base installation works without
credentials or network-backed integrations after installation.

## Choose what you need

- Use the base package for local workspace, pursuit, task, and skill workflows.
- Add the `google` profile for Gmail and Google APIs.
- Add the `llm` profile for supported AI-assisted workflows.
- Add the `web` profile for the local web interface.
- Add `chrome-auth` for supported browser credential-store integration.
- Add `all` for the complete supported feature set.

See [Integrations and profiles](integrations.md) before enabling an integration, and read
[Local data and privacy](privacy.md) to understand what fieldkit stores and sends.

## Find an answer

- [What fieldkit does](user-guide.md) explains the product and its main workflows.
- [How-to guides](guides/init.md) walk through specific tasks.
- [CLI reference](cli-reference.md) documents commands and options generated from the code.
- [Troubleshooting](reference/troubleshooting.md) maps symptoms to safe next steps.
- [Compatibility](compatibility.md) states the supported and experimental environments.
- [Architecture and decisions](design/local-first-cli.md) explains the design constraints that contributors preserve.

To contribute, start with the repository's
[contribution guide](https://github.com/mpeter/fieldkit-cli/blob/main/CONTRIBUTING.md).
