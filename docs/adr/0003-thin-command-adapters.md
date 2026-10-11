---
status: Accepted
applies_to: fieldkit-cli
---

# Thin command adapters

## Context

Command modules need a consistent boundary between CLI syntax and product
behavior.

## Decision

Commands parse input, call domain behavior, render output, and return its exit
status. Shared behavior belongs in the domain rather than a command adapter.

Only `cli_exit.cli_main()` and the `__main__.main()` dispatcher end the process.
A command reports failure by raising a typed `FieldkitError`, which
`handle_cli_exception()` maps to the documented exit code. Domain code never
exits. Command sites that predate this rule and still raise `SystemExit` or call
`ctx.exit()` are counted per enclosing function in `.exit-sites-baseline.json`;
`scripts/check_exit_sites.py` fails if a count rises, a site moves or appears in
another function, or domain code exits, and each migration lowers the baseline.

## Consequences

Hooks, tests, and future commands reuse one implementation, and error handling
has a single authoritative mapping. Changing how a failure is reported, such as
its message or a structured error format, happens once in `cli_exit.py` for
every migrated command.
