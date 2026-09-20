# retry-policy Specification

## Purpose
Define the current behavioral contract for retry-policy, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Single home for retry policy

Retry policy MUST be defined once, in `src/fieldkit/config/retry.py`, covering maximum
attempts, backoff bounds, and the retryable HTTP status set. Call sites MUST NOT
inline these values as literals on a `@retry` decorator.

Neither the factories nor the constants MAY be re-exported from
`fieldkit.config.__init__`. `fieldkit.config` is a pure foundation imported on every
CLI invocation and today pulls neither `httpx` nor `tenacity`; re-exporting would add
their import cost (measured: 232ms and 51ms cumulative) to every command, including
ones that never open a socket. The module is therefore public (`retry.py`, not
`_retry.py`) and imported directly by the modules that need it — all of which already
import an HTTP client, so they pay no new cost.

A deviation from a policy default (for example a longer backoff ceiling for a
quota-limited API) MUST be expressed as an explicit named argument at the call site
with a comment stating the reason. An unexplained numeric difference between two call
sites is a violation.

#### Scenario: Call site uses the shared policy
- **GIVEN** a module adding retry to a remote call
- **WHEN** it applies a retry decorator
- **THEN** the decorator is imported from `fieldkit.config.retry`
- **AND** no attempt count or backoff bound appears as a literal at the call site

#### Scenario: Base config import stays cheap
- **GIVEN** a command that makes no network call
- **WHEN** it imports `fieldkit.config`
- **THEN** neither `httpx` nor `tenacity` is present in `sys.modules` as a result

#### Scenario: Deviation is named and justified
- **GIVEN** the Gmail sync path, which needs a longer ceiling for quota backoff
- **WHEN** it applies the retry decorator
- **THEN** the non-default ceiling is passed as an explicit keyword argument
- **AND** a comment states why the default is insufficient

### Requirement: Retry factories are split by call-site safety

The module MUST expose exactly two decorator factories, and a call site MUST select
between them on the idempotency of the wrapped call.

`transient_retry(predicate, logger, ...)` MAY be used **only** where repeating the
call is harmless. It retries the full transient set: connect-phase failures, timeouts,
and the statuses in `RETRY_TRANSIENT_STATUSES`.

`connect_retry(logger, ...)` MUST be used everywhere else. It retries **only**
connect-phase failures (`httpx.ConnectError`, `httpx.ConnectTimeout`), where the
request provably never reached the server.

The rationale is that retrying is at-least-once: a timeout or lost response after the
request has been transmitted does not prove the server did not act on it.

Idempotency MUST be judged on the **operation's effect, not its HTTP verb**. A `PATCH`
that assigns absolute field values is safe to repeat and qualifies for
`transient_retry`; a `PATCH` that increments a counter or appends to a list does not.
A `POST` that creates a resource does not. Where the judgement is not obvious from the
call, the call site MUST record it in a comment — the defect this requirement exists to
prevent is not a wrong choice but an unrecorded one, which the next author copies.

#### Scenario: Verb alone does not decide the factory
- **GIVEN** two calls using the same HTTP method
- **WHEN** one assigns absolute values and the other appends to a collection
- **THEN** the first MAY use `transient_retry`
- **AND** the second MUST use `connect_retry`

#### Scenario: Idempotent read retries the full transient set
- **GIVEN** a `GET` request to a remote API
- **WHEN** the server returns HTTP 503
- **THEN** the call is retried
- **AND** it succeeds if a later attempt returns 200

#### Scenario: Non-idempotent write does not retry a lost response
- **GIVEN** a `POST` that creates a server-side resource
- **WHEN** the request is transmitted and the response is lost to a read timeout
- **THEN** the call is NOT retried
- **AND** the underlying exception propagates to the caller
- **AND** no duplicate resource is created

#### Scenario: Non-idempotent write retries a failure to connect
- **GIVEN** a `POST` that creates a server-side resource
- **WHEN** the connection is refused before the request is transmitted
- **THEN** the call IS retried, because the server provably did not receive it

### Requirement: Retry observability is not optional

Both factories MUST attach per-attempt logging at `WARNING` before each backoff
sleep. It MUST NOT be possible to obtain a retry decorator from this module without
it. Per-attempt logging MUST NOT be an argument a caller can disable.

This is a structural requirement, not a convention: eight of the nine retry sites
that existed before this change omitted per-attempt logging, so a rule that relies on
authors remembering has already been shown to fail.

#### Scenario: Successful retry is observable
- **GIVEN** a wrapped call whose first attempt fails transiently and whose second succeeds
- **WHEN** the call completes successfully
- **THEN** a `WARNING` was emitted before the backoff sleep
- **AND** the caller receives the successful result

#### Scenario: Logging cannot be suppressed
- **GIVEN** the public surface of `fieldkit.config`
- **WHEN** a caller obtains a retry decorator
- **THEN** no parameter exists that disables per-attempt logging

### Requirement: Retry exhaustion is distinguishable from single-attempt failure

When retries are exhausted, the **operator-visible failure report** MUST state that
multiple attempts were made and how many. This obligation is met by the factory
itself, at `ERROR`, and MUST NOT depend on what a calling module does with the
exception message — a report identical to that produced by a non-retried failure is a
violation.

Factories MUST re-raise the underlying exception rather than a wrapper type, so
existing `except` clauses and the `fieldkit.errors` exception→exit-code mapping remain
correct. Because the re-raised exception's own message therefore carries no attempt
count, the factory MUST additionally attach the count to the exception as a note for
traceback context.

Conversion sites MAY include the attempt count in the typed error they raise,
following the precedent at `sf/client.py:363`. That is a convention and MUST NOT be
the only place the count appears.

#### Scenario: Exhaustion is reported by the factory, not the caller
- **GIVEN** a wrapped call that fails transiently on every attempt
- **WHEN** the final attempt fails
- **THEN** an `ERROR` is logged stating the number of attempts made
- **AND** that log is emitted regardless of how the caller formats its own error
- **AND** it is distinguishable from the report produced by the same call failing once

#### Scenario: Attempt count reaches the traceback
- **GIVEN** a wrapped call that exhausts its attempts
- **WHEN** the exception propagates uncaught
- **THEN** the traceback carries a note stating the number of attempts made

#### Scenario: Underlying exception type is preserved
- **GIVEN** a wrapped call that exhausts its attempts on `httpx.ConnectError`
- **WHEN** the final attempt fails
- **THEN** the exception reaching the caller is `httpx.ConnectError`, not a retry wrapper
- **AND** the caller's existing `except httpx.RequestError` clause still matches

### Requirement: One definition of a transient status

`RETRY_TRANSIENT_STATUSES` MUST be the single definition of which HTTP statuses are
transient. Domain predicates that classify library-specific exceptions
(`httpx.HTTPStatusError`, googleapiclient `HttpError`) MUST source their status set
from this constant rather than declaring their own.

Domain predicates themselves MUST remain in their own domain modules. They MUST NOT
be hoisted into `config/`, which would require `config/` to import third-party client
libraries and invert the dependency direction enforced by `tach`.

#### Scenario: Predicates agree on transience
- **GIVEN** the Salesforce, Gmail, and Google Docs transient predicates
- **WHEN** each is asked about HTTP 502 and HTTP 504
- **THEN** all three classify them identically as transient

#### Scenario: Permanent statuses are not retried
- **GIVEN** any domain transient predicate
- **WHEN** it is asked about HTTP 401, 403, or 404
- **THEN** it returns false
- **AND** the wrapped call is attempted exactly once

#### Scenario: config does not depend on client libraries
- **GIVEN** the module `fieldkit.config.retry`
- **WHEN** its imports are inspected
- **THEN** it imports no third-party API client library
- **AND** `tach check` passes
