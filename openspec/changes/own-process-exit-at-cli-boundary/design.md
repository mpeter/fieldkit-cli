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
- **Ratchet instead of a single migration.** Recorded counts make each
  domain's migration independently reviewable while preventing new sites from
  the first change.
- **Counts per enclosing function.** A per-file total would let a migration
  remove one site and add another elsewhere in the same file unnoticed. Counting
  by qualified function name makes that a mismatch, and a count below its
  baseline fails until the baseline is lowered. `--write-baseline` refuses a
  file whose total grew or any domain exit; it records a site moved within its
  file, so the move shows up in the baseline diff for review rather than
  passing silently.
- **The baseline itself is checked against the base revision.** A hand-edited
  count increase would otherwise pass, since the check trusts the committed
  file. `--base` compares it with the baseline at the merge-base and rejects
  any file whose total grew. `make pr-check` passes `QUALITY_BASE`, and the
  pull-request `Lint (ruff)` job passes the base tip of the merge commit, as the
  CRAP gate does. Line numbers are deliberately not part of the identity,
  since unrelated edits shift them.
- **Scope.** The check scans all of `src/fieldkit/` with the AST. It exempts
  only `cli_exit.cli_main()`, `__main__.main()` and the body of an exact
  module-level `if __name__ == "__main__":` block, which is a separate process entry point;
  the block's `else` branch runs on import and is scanned. Exit functions and
  exceptions imported under another name from `sys`, `os`, `builtins` or
  `click` are recognised. `scripts/` and `hooks/` are standalone programs
  and keep their own `sys.exit()`.

## Risks

- The AST match counts any `*.exit()`/`*._exit()` call, which could flag an
  unrelated method named `exit`. None exists today; a false positive fails
  loudly rather than hiding a site.
