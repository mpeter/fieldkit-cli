# Product model

fieldkit combines a local workspace with optional external integrations. The command-line interface
is the stable entry point; Markdown, YAML, JSON, and SQLite files provide inspectable local state.

## Persistent roots and harness scratch

fieldkit keeps three persistent kinds of content separate:

- the installed application and its packaged templates;
- the workspace you intentionally edit and version; and
- runtime artifacts such as caches, logs, and integration state.

Commands resolve these roots through configuration rather than assuming a particular username,
checkout, organization, or workstation layout.

Agent and health harnesses use a fourth, cache-class location for disposable source worktrees.
`FIELDKIT_HARNESS_ROOT` overrides that location. Otherwise fieldkit uses
`$XDG_CACHE_HOME/fieldkit` or `~/.cache/fieldkit`. The harness can regenerate this content from the
source repository, so it does not belong in the workspace or runtime-data backup contract.

## Portable core and optional capabilities

The base package supports local workflows and an offline first-success path. External systems are
capabilities you choose. Installing an integration profile adds its SDKs; configuring it enables its
behavior. An integration that you have not configured is not a broken installation.

When a selected command needs a profile that is not installed, fieldkit exits with a data/config
error and prints copyable installation guidance. Authentication failures remain distinct so scripts
can tell user action from invalid input.

## Local-first does not mean offline-only

fieldkit stores its working state locally, but configured integrations can read or send data to
their providers. Commands that use Gmail, Salesforce, an LLM, or another service cross that service's
trust boundary. Review [Local data and privacy](privacy.md) and the relevant integration guide before
using real account data.

## Public compatibility surface

For 1.0, documented CLI commands and options, exit codes, machine-readable JSON, configuration keys,
persisted schema versions, and explicitly public Python imports form the compatibility contract.
Undocumented modules and internal file layout may change between minor releases.
