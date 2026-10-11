## Context

`handle_cli_exception()` maps typed exceptions to the four canonical exit codes.
The dispatcher in `__main__.main()` also accepts a returned integer and a
`SystemExit`, so direct exits produce correct codes today; they just bypass the
mapping.

## Decisions

- **Typed exceptions, not returned codes, as the target.** The dispatcher
  honours a returned integer, but Click's `CliRunner` reports exit 0 for a
  command that returns 3, so returned codes would make command tests silently
  misleading. Exceptions keep the reason, the message and the code together and
  reach the one mapping.
- **Ratchet instead of a single migration.** Per-file counts make each
  domain's migration independently reviewable while preventing new sites from
  the first change.
- **Stale counts fail.** A count below its baseline fails until the baseline is
  lowered, so a removed site can't be silently spent on a new one elsewhere in
  the file. `--write-baseline` refuses to record growth.
- **Scope.** The check scans `src/fieldkit/` with the AST, skipping
  `cli_exit.py`, `__main__.py` and `if __name__ == "__main__":` blocks, which are
  separate process entry points. `scripts/` and `hooks/` are standalone programs
  and keep their own `sys.exit()`.

## Risks

- The AST match counts any `*.exit()`/`*._exit()` call, which could flag an
  unrelated method named `exit`. None exists today; a false positive fails
  loudly rather than hiding a site.
