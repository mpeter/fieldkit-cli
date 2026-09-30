# Contributor patterns

When adding a command or a file-producing workflow, find the closest pattern
below, follow its owner, and extend its focused regression test. Run the cited
test before changing a shared persistence path.

- New command adapter: see `src/fieldkit/commands/sf/` for parsing CLI input,
  calling domain behavior, rendering output, and returning the domain exit status.
- Configured filesystem write: see `src/fieldkit/config/` for resolving a
  configured root and using the established atomic write helper.
- Optional integration: see `src/fieldkit/commands/gmail/` for delaying imports
  until command invocation and presenting a clear missing-profile error.
- Persistent pursuit data: see `src/fieldkit/pursuit/` for the canonical
  frontmatter I/O layer.
- Interruption-safe multi-file output: save exact decisions with
  `fieldkit.ingest.prepared.save_prepared` before publishing files. Let
  `fieldkit.ingest.replay.replay_prepared` apply the saved note, pursuit, and task
  effects; `complete_prepared` records completion only after those effects
  succeed. Keep model calls and task classification outside replay. Start with
  `tests/test_ingest_prepared_store.py::test_replay_failure_retains_intent` and
  `tests/test_ingest_run_lock.py::test_busy_run_has_no_database_or_discovery_effects`.
- Source-owned Markdown: `fieldkit.util.owned_markdown` recognizes actual
  section entries and validates versioned markers. Task and pursuit publishers
  own rendering and conflict policy. When changing an effect, inspect
  `read_owned_markers` and extend
  `tests/test_owned_markdown.py::test_non_owned_examples_are_excluded`; do not
  infer ownership from a substring or an identical visible sentence.
- Shared file publication: `fieldkit.util.atomic` owns bounded locks,
  runtime-lock directory preparation, atomic replacement, and exclusive atomic
  creation. `fieldkit.util.text_snapshot` owns bounded regular-file reads.
  Wrap the entire read/check/write operation in the target lock, keep runtime
  lock files outside the workspace, and use
  `fieldkit.util.workspace_paths.resolve_workspace_output` for ingest
  destinations so child redirects are rejected. Start with
  `tests/test_text_snapshot.py::test_snapshot_refuses_change_during_read` and
  `tests/test_atomic_text_create.py::test_atomic_create_refuses_existing_destination`.
- Failure-path test: see `tests/test_version_cli.py` for asserting the direct
  command result and rendered user-facing behavior.

These examples are implementation references, not public compatibility promises
for their internal module layout. The contributor guide defines the stable
rules; the referenced implementations show how those rules are applied today.

For the limits of these guarantees, read the
[safe persistence decision](../adr/0004-safe-persistence.md#scope-and-current-implementation).
