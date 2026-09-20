# fieldkit-gaze-visibility Specification

## Purpose
Define the current behavioral contract for fieldkit-gaze-visibility, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: python-custom.md includes CR-014 gaze-visible assertion pattern

The `python-custom.md` in fieldkit MUST include CR-014 documenting the gaze-visible
assertion pattern — the requirement that tests must directly reference the bound return
value for the gaze quality pipeline's assertion mapper to credit them with contract coverage.

Note: CR-001 through CR-013 already exist in `python-custom.md`. The new rule is CR-014
(not CR-007, which is already occupied by the `No shell=True in subprocess` rule).

#### Scenario: CR-014 is present with correct and incorrect examples
- **GIVEN** the fieldkit `python-custom.md` pack has been updated per this spec
- **WHEN** an agent reads fieldkit's `python-custom.md`
- **THEN** it finds CR-014 with `[MUST]` severity
- **AND** it finds a CORRECT example: `result = fn(...); assert result`
- **AND** it finds a WRONG example: `result = fn(...); derived = list(result); assert x in derived`
- **AND** it explains that Pass 1 binding requires the assertion to directly reference the bound variable

#### Scenario: pytest.raises note is included
- **GIVEN** the fieldkit `python-custom.md` pack has been updated per this spec
- **WHEN** an agent reads CR-014
- **THEN** it finds a note that `pytest.raises(SomeError)` only earns contract coverage when the production function has a `raise` statement in its own AST body

#### Scenario: CliRunner note is included
- **GIVEN** the fieldkit `python-custom.md` pack has been updated per this spec
- **WHEN** an agent reads CR-014
- **THEN** it finds a note that `assert result.exit_code == N` satisfies CR-014 for CliRunner tests

#### Scenario: python-custom.md version is bumped
- **GIVEN** the fieldkit `python-custom.md` pack has been updated per this spec
- **WHEN** the version frontmatter is read
- **THEN** the `version:` field is `2.1.0` (MINOR bump: new rule CR-014 added, no existing rules modified)

### Requirement: testing-patterns skill includes GazeCRAP Visibility section

The `testing-patterns` skill in fieldkit MUST include a `## GazeCRAP Visibility`
section explaining the direct-reference assertion pattern with a quick-reference code
block.

#### Scenario: GazeCRAP Visibility section is present
- **GIVEN** the fieldkit `testing-patterns/SKILL.md` has been updated per this spec
- **WHEN** the testing-patterns skill is loaded in fieldkit context
- **THEN** it includes a `## GazeCRAP Visibility` section
- **AND** it includes a quick-reference code block showing VISIBLE patterns (✓) and INVISIBLE anti-pattern (✗)
- **AND** it includes the command: `uv run gazepy quality src/fieldkit/ --tests tests/`
