## Why

`AGENTS.md` said only `cli_exit.cli_main()` calls `sys.exit()`, and ADR 0003
says error handling has one authoritative mapping. In practice 231 sites in 70
command modules end the process directly with `raise SystemExit(EXIT_*)` or
`ctx.exit()`, bypassing `handle_cli_exception()` (#113). The rule was false, so
it prevented nothing, and every change to failure reporting has to be repeated
at each site.

Migrating every site at once would touch most command modules and roughly 900
test assertions: Click's `CliRunner` discards a command's return value, so tests
built around `SystemExit` need reworking alongside the code.

## What Changes

1. State the target rule: only `cli_main()` and the `__main__.main()` dispatcher
   end the process. Commands report failure by raising a typed `FieldkitError`
   that `handle_cli_exception()` maps. Domain code never exits.
2. Add `scripts/check_exit_sites.py` and the committed per-file
   `.exit-sites-baseline.json`. The check fails when domain code exits, a
   command file's count rises, a file without an entry adds a site, or a count
   falls without the baseline being lowered. It runs as the `exit-sites` stage of
   `make pr-check` and `make quality-full`.
3. Update `AGENTS.md`, ADR 0003 and the exit-code reference to match, and record
   the deliberate `print()` use in `cli_exit.py`.
4. Migrate the existing sites in later per-domain changes, each lowering the
   baseline.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `cli-exit-taxonomy`: process termination belongs to the CLI boundary, and
  direct exits outside it are ratcheted.

## Impact

Contributor tooling and documentation only; no command's exit code or output
changes.
