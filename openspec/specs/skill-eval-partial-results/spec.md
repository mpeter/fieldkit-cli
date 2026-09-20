# skill-eval-partial-results Specification

## Purpose
Define the current behavioral contract for skill-eval-partial-results, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: One per-skill judge failure preserves the batch

During a multi-skill behavioral evaluation, the runner SHALL contain a dedicated
`JudgeResponseError` at the current skill boundary, preserve all
successful judgments, continue with later skills, emit an explicit error entry
for the failed skill, and return exit 1 after rendering the report.

#### Scenario: middle skill fails

- **GIVEN** three skills with behavioral cases and a judge that raises `JudgeResponseError` for the second skill
- **WHEN** the behavioral batch runs in JSON mode
- **THEN** `behavioral_results` contains the first and third judgments in input order, `behavioral_errors` identifies only the second skill, stdout is valid JSON, and the command returns 1

#### Scenario: error detail stays private

- **GIVEN** a `JudgeResponseError` whose message contains model response text
- **WHEN** the runner records and renders the failure
- **THEN** JSON and stderr contain only the skill name, the general category, and a fixed error code, with none of the exception text or judge response

### Requirement: Global LLM failure classifications still propagate

Provider-originated `LLMError` values SHALL NOT be contained as per-skill errors,
including `general`, authentication, and rate-limit categories. They SHALL
propagate to the canonical CLI exception mapper so general provider failures
return exit 3, authentication returns exit 2, and rate limiting returns exit 1.
Unexpected exception types SHALL retain the existing data-error path.

#### Scenario: general provider failure propagates

- **GIVEN** the provider layer raises a general `LLMError` with an original transport or runtime exception
- **WHEN** the behavioral batch runs
- **THEN** the exception propagates immediately, no later judge call occurs, and `cli_main()` maps it to exit 3

#### Scenario: authentication fails

- **GIVEN** the judge raises an authentication-classified `LLMError` on any skill
- **WHEN** the behavioral batch runs
- **THEN** the exception propagates immediately, no later judge call occurs, and `cli_main()` maps it to exit 2

#### Scenario: rate limit is exhausted

- **GIVEN** the judge raises a rate-limit-classified `LLMError`
- **WHEN** the behavioral batch runs
- **THEN** the exception propagates immediately and `cli_main()` maps it to exit 1

### Requirement: Zero-case skills do not invoke the judge

A selected skill whose loaded eval data contains zero behavioral cases SHALL be
reported as `without_evals` and skipped before `judge_skill()` is called. This
condition SHALL NOT by itself make the command partial.

#### Scenario: explicitly selected skill has no cases

- **GIVEN** an explicitly selected skill with a missing, invalid, or empty behavioral case list
- **WHEN** behavioral evaluation runs
- **THEN** no judge call is made for that skill, the report includes it under `behavioral_without_evals`, and the command remains successful if no other failure occurs

#### Scenario: mixed covered and empty skills

- **GIVEN** a batch containing one zero-case skill and two successfully judged skills
- **WHEN** the report is rendered
- **THEN** the attempted count is three, the with-evals count is two, the without-evals count is one, and both successful judgments are preserved

### Requirement: Existing verdict semantics remain stable

Contained judge failures SHALL be additive to the existing exit decision. Any
`not-covered` verdict returns exit 1; `unclear` remains advisory and returns 0
when no partial condition exists; clean or stub judgments return 0. Static skill
evaluation output and exit behavior SHALL remain unchanged.

#### Scenario: unclear-only batch

- **GIVEN** every judge call succeeds and at least one verdict is `unclear`, with no `not-covered` verdict
- **WHEN** the behavioral batch completes
- **THEN** it emits the existing advisory and returns 0

#### Scenario: contained error and not-covered coexist

- **GIVEN** one contained general judge error and one successful judgment containing a `not-covered` verdict
- **WHEN** the batch completes
- **THEN** both conditions are represented in the report and the command returns 1
