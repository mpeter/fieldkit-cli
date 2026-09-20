# Contributor patterns

Use these current examples before introducing a second way to solve the same
problem.

- New command adapter: see `src/fieldkit/commands/sf/` for parsing CLI input,
  calling domain behavior, rendering output, and returning the domain exit status.
- Configured filesystem write: see `src/fieldkit/config/` for resolving a
  configured root and using the established atomic write helper.
- Optional integration: see `src/fieldkit/commands/gmail/` for delaying imports
  until command invocation and presenting a clear missing-profile error.
- Persistent pursuit data: see `src/fieldkit/pursuit/` for the canonical
  frontmatter I/O layer.
- Failure-path test: see `tests/test_version_cli.py` for asserting the direct
  command result and rendered user-facing behavior.

These examples are implementation references, not public compatibility promises
for their internal module layout. The contributor guide defines the stable
rules; the referenced implementations show how those rules are applied today.
