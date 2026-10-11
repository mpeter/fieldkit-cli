## ADDED Requirements

### Requirement: Process termination belongs to the CLI boundary

Only `cli_exit.cli_main()` and the `__main__.main()` dispatcher SHALL end the
fieldkit process. Domain code under `src/fieldkit/` outside `commands/` MUST NOT
raise `SystemExit` or call an exit function. Command adapters SHALL report
failure by raising a typed exception that `handle_cli_exception()` maps. Direct
exits that predate this rule MUST be recorded per enclosing function in a
committed baseline, and the contributor quality gate MUST fail when any function
exceeds its recorded count or falls below it without the baseline being lowered.

#### Scenario: A change adds a direct exit to a command
- **GIVEN** a command module that raises `SystemExit(EXIT_DATA)` in a new place
- **WHEN** `make pr-check` runs
- **THEN** the `exit-sites` stage SHALL fail
- **AND** its output SHALL name the file, the lines, the function and the baseline count

#### Scenario: A change moves a direct exit to another function
- **GIVEN** a command module that removes a direct exit from one function and adds one to another
- **WHEN** the exit-site check runs
- **THEN** it SHALL fail and name both functions

#### Scenario: Domain code exits
- **GIVEN** a module outside `commands/` that calls `sys.exit()`
- **WHEN** the exit-site check runs
- **THEN** it SHALL fail whatever the baseline records

#### Scenario: A migration removes direct exits
- **GIVEN** a command module whose direct exits were replaced by typed exceptions
- **WHEN** the exit-site check runs before the baseline is lowered
- **THEN** it SHALL fail and direct the contributor to `--write-baseline`
- **AND** `--write-baseline` SHALL record the lower count but refuse to record any increase
