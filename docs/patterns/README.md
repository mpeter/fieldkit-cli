# Contributor patterns

Use these current examples before introducing a second way to solve the same
problem.

- New command adapter: see `src/fieldkit/commands/meeting/link_cmd.py` for
  parsing CLI input, calling domain behavior in `src/fieldkit/meeting/`,
  rendering text or JSON, and running inside `cli_main()` so domain exceptions
  map to the shared exit codes in `fieldkit.cli_exit`.
- Configured filesystem write: see `src/fieldkit/config/` for resolving a
  configured root, and `src/fieldkit/util/atomic.py` for the established atomic
  write helpers.
- Optional integration: see `src/fieldkit/commands/gmail/` for delaying imports
  until command invocation and presenting a clear missing-profile error.
- Persistent pursuit data: see `src/fieldkit/pursuit/` for the canonical
  frontmatter I/O layer.
- Failure-path test: see `tests/test_version_cli.py` for asserting the direct
  command result and rendered user-facing behavior.

These examples are implementation references, not public compatibility promises
for their internal module layout. The contributor guide defines the stable
rules; the referenced implementations show how those rules are applied today.
