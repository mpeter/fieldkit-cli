# fieldkit contributor agent guide

fieldkit is a Python command-line application for local-first sales and account workflows.
This repository builds the `fieldkit` command; user workspaces and runtime data live outside the checkout.
This file records project constraints that are difficult to infer from the tree; the same contribution path works for humans and coding agents.

## Start here

```bash
make bootstrap                    # locked environment and contributor hooks
make pr-check                     # supported bounded pull-request gate
uv run pytest tests/ -q           # complete suite when broader evidence is needed
make quality-full                 # maintainer/post-merge enforcement
make docs                         # regenerate CLI and dependency references
```

Use Python 3.11 or newer. The project uses `uv`, Hatchling, Click, strict mypy, Ruff, Tach, pytest, and xdist.
Run commands from the repository root. When testing worktree changes, use `uv run fieldkit`; a separately installed `fieldkit` command may execute another checkout.

## Architecture boundaries

- Domain behavior belongs under `src/fieldkit/<domain>/`. Modules under
  `commands/` are thin adapters: parse input, call the domain, render output,
  and return its exit status.
- Import direction is `commands -> domain <- hooks`. Shared behavior does not
  belong in command modules.
- The lazy command dispatcher must not import optional integration packages
  until their command is invoked. Keep the base installation usable without
  Salesforce, Google, LLM, web, or browser-auth dependencies.
- Do not conflate the code root, user workspace, and runtime-data root. Resolve
  them through `get_fieldkit_root()`, `get_fieldkit_home()`, and
  `get_fieldkit_data()`.
- Cross-module mappings use a `TypedDict` or dataclass. Fixed-value strings use
  `Literal`. A `cast()` needs an adjacent runtime type check.
- CLI output uses `click.echo()` or Rich rather than `print()`. Text file I/O
  uses `pathlib.Path` with an explicit UTF-8 encoding.
- Constants have one authoritative home. Extend the existing domain table or
  policy instead of copying it into a command or test.

## Safety and behavioral invariants

- Quality thresholds, action pins, severity definitions, and architecture
  boundaries are release controls. Do not weaken a gate to make a change pass.
- Exit statuses are `0` success, `1` partial/retryable, `2` authentication or
  user action required, and `3` invalid data or usage. Only
  `cli_exit.cli_main()` calls `sys.exit()`. Click usage errors are normalized by
  the top-level handler; domain code must not raise them.
- Do not add shell scripts, `shell=True`, or shell-composed directory changes.
  Every subprocess, network request, and LLM call needs a named timeout.
- Runtime writes are limited to the configured workspace, runtime-data root,
  and documented fieldkit configuration/data locations. Validate user-derived
  paths before writing.
- Multi-writer JSON state uses `locked_json_update()`. Single-writer state uses
  a temporary file plus `Path.replace()`. Configuration uses
  `atomic_yaml_write()`.
- Pursuit frontmatter is read and written through `fieldkit.pursuit.io`.
  Render YAML scalars with the existing helpers; string interpolation can turn
  untrusted text into a document separator.
- Credentials, cookies, customer data, email content, private hosts, personal
  email addresses, usernames, and absolute home paths do not belong in commits,
  fixtures, logs, or diagnostics. Use fictional examples such as
  `example.com` and `acme-corp.com`.
- External text added to an LLM prompt needs a length bound or content guard.
  New credential files require mode `0600`.
- Authentication exceptions propagate to the top-level CLI handler. Do not
  convert a dead credential into a partial success that an orchestrator will
  retry forever.
- Optional integrations degrade cleanly. LLM consumers provide a documented
  no-LLM path, and file-producing commands verify non-empty output before
  reporting success.
- Refactors remove the old implementation. Do not leave compatibility shims or
  re-export aliases unless a public compatibility policy explicitly requires
  them.

## Testing rules that prevent recurring failures

- New behavior needs happy-path and failure-path coverage; a bug fix needs a
  regression test. Mark tests `unit`, `integration`, or `characterization`.
- Use `tmp_path` for filesystem tests. Tests must not inherit real fieldkit
  configuration, credentials, user data, or network access.
- Patch the name imported by the module under test, not its original definition.
- A new cached configuration accessor must be cleared by
  `clear_config_caches()`.
- Use `pytest.raises(..., match=...)` or assert on the captured exception.
  Parameterize cases instead of looping through them in one test.
- A new watcher leaf must join the autouse path-isolation fixture before its
  tests run.
- Use `datetime.now(tz=UTC).date()`, not `date.today()`.
- At least one assertion should reference a function's direct return value
  before assertions on values derived from it. This preserves contract-coverage
  visibility.
- Tests for permission failures skip when the effective user is root because
  root can bypass ordinary mode-bit checks.

After changing Python, run the single-file lint and type checks immediately:

```bash
uv run ruff check path/to/file.py
uvx pyright path/to/file.py
```

Before a pull request, run `make pr-check`; its full-project mypy pass catches
cross-module errors that a single-file check cannot. Changes to CLI syntax also
require `make docs`. The complete gate runs on protected automation and can be
invoked locally with `make quality-full` when the change warrants it.

## Documentation and change records

- Public documentation is task-first: state what the reader can do, show the
  supported command, describe the expected result, then link to detail.
- User-visible changes add one descriptive Markdown fragment under
  `changelog.d/`. A public issue number is optional; private tracker identifiers
  and organization-specific context are forbidden.
- Do not edit `CHANGELOG.md` for an ordinary pull request. Release preparation
  assembles fragments atomically.
- Keep internal audit, handoff, succession, work-order, and operator material
  outside the rendered public documentation set and eventual public export.
- Update this guide only for non-obvious, repository-wide constraints. Put
  domain-specific guidance beside that domain.

## Git and review contract

- Work on a feature branch and submit a pull request; never commit directly to
  `main`.
- Preserve unrelated work in a dirty checkout. Use an isolated worktree when
  concurrent work is possible, and verify the worktree, branch, and status
  before committing or pushing.
- Keep changes reviewable and give each commit one purpose. If a task expands
  into several unrelated areas, stop and split it.
- Pull requests explain purpose, observable behavior, verification evidence,
  documentation/changelog impact, and privacy or security impact.
- Security, release, workflow, governance, and quality-policy changes require
  explicit maintainer review. Independent code-owner approval becomes mandatory
  after the project has a second maintainer with review authority.

## Public design references

When a change alters a component boundary, data flow, persistent format, or security boundary, review and update the applicable design document under `docs/design/` or decision record under `docs/adr/`.
For reusable implementation, follow the pattern in `docs/patterns/README.md` before creating a parallel convention.

## Optional accelerators

Standard Git, filesystem search, Python tooling, and committed documentation are sufficient to contribute.
Some maintainers use semantic code search, local knowledge indexes, contract-coverage authoring tools, or multi-agent review commands.
Use them when available, but never make their availability a contributor requirement or substitute their output for `make pr-check`.

For common patterns, start with the existing Salesforce command adapter and client, pursuit-stall watcher, and nearby unit tests.
Reference documentation for exit codes, configuration, compatibility, and integrations is linked from the public documentation site.
