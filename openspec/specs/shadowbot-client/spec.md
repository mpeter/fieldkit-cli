# shadowbot-client Specification

## Purpose
Define the current behavioral contract for shadowbot-client, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: LangGraph thread creation

The client SHALL create a thread via `POST /threads` to the configured
`shadowbot.api_base`, persist `thread_id` to
`<state-dir>/shadowbot-state.json`, and reuse it on subsequent calls.

`POST /threads` creates a server-side resource and is therefore **non-idempotent**.
It MUST use connect-phase retry only: a failure to establish the connection MAY be
retried, because the server provably did not receive the request. A timeout or
transport failure occurring after the request has been transmitted MUST NOT be
retried, because the server may already have created the thread and a retry would
orphan it.

HTTP 5xx responses MUST NOT be retried on this endpoint. This is a deliberate
exclusion, not an omission: a 5xx proves the request reached the server, so retrying
risks a duplicate thread. Accepting a hard failure during a transient server outage
is the chosen trade against silently leaking threads. Revisiting it requires
server-side idempotency-key support.

Previously: `POST /threads` had no retry at all; PR 1607 proposed retrying
`httpx.TimeoutException` and `httpx.RequestError`, both of which include failures
occurring after transmission, and so would have duplicated threads on a lost
response.

#### Scenario: Connection refused is retried
- **GIVEN** `query()` is called with no persisted `thread_id`
- **WHEN** the first `POST /threads` fails with `httpx.ConnectError`
- **AND** a subsequent attempt succeeds
- **THEN** exactly one thread is created
- **AND** a `WARNING` was emitted before the backoff sleep
- **AND** the returned `thread_id` is persisted

#### Scenario: Lost response is not retried
- **GIVEN** `query()` is called with no persisted `thread_id`
- **WHEN** `POST /threads` is transmitted and then fails with `httpx.ReadTimeout`
- **THEN** `POST /threads` is attempted exactly once
- **AND** `ShadowbotQueryError` is raised
- **AND** no second thread is created

#### Scenario: Server error is not retried
- **GIVEN** `query()` is called with no persisted `thread_id`
- **WHEN** `POST /threads` returns HTTP 503
- **THEN** `POST /threads` is attempted exactly once
- **AND** `ShadowbotQueryError` is raised with the status code

#### Scenario: Exhausted connect retry reports attempt count
- **GIVEN** `query()` is called with no persisted `thread_id`
- **WHEN** every `POST /threads` attempt fails with `httpx.ConnectError`
- **THEN** `ShadowbotQueryError` is raised
- **AND** an `ERROR` is logged stating the number of attempts made
- **AND** the report is distinguishable from the same call failing once

#### Scenario: Stale thread recovery is unaffected
- **GIVEN** a persisted `thread_id` that no longer exists on the server
- **WHEN** `POST /threads/{id}/runs/stream` returns HTTP 404
- **AND** no connect-phase failure occurs
- **THEN** the total count of `POST /threads` calls is 2, as before
- **AND** connect retry applies independently within each of those calls

### Requirement: LangGraph streaming query
The client SHALL POST to `/threads/{thread_id}/runs/stream` with JSON body:
```json
{
  "assistant_id": "sales_assistant_v2",
  "input": {"messages": [{"role": "human", "content": "<prompt>"}]},
  "stream_mode": ["values"]
}
```
and parse the SSE response to return a `ShadowbotResponse` whose `content` is the
text of the last AI message in the final `event: values` event.

#### Scenario: Successful streaming query
- **WHEN** API returns 200 SSE stream
- **THEN** content of last AI message across all `event: values` events returned as `ShadowbotResponse.content`

#### Scenario: 401 on stream
- **WHEN** stream endpoint returns 401
- **THEN** `ShadowbotAuthError` raised

#### Scenario: Non-SSE Content-Type on stream response
- **WHEN** stream endpoint returns 200 but `Content-Type` is not `text/event-stream`
- **THEN** `ShadowbotQueryError` is raised immediately without attempting to parse the body

#### Scenario: Non-401 error on stream
- **WHEN** stream endpoint returns non-200, non-401 (excluding 404 handled above)
- **THEN** `ShadowbotQueryError` raised

#### Scenario: Timeout
- **WHEN** request exceeds configured timeout
- **THEN** `ShadowbotQueryError` raised

### Requirement: Pure SSE parser
`_parse_sse_stream(lines: Iterable[str]) -> ShadowbotResponse` MUST be a pure
function with no network dependencies.

#### Scenario: String content extracted
- **WHEN** final `values` event has `messages[-1].type == "ai"` and `content` is a string
- **THEN** `ShadowbotResponse.content` equals that string

#### Scenario: List content joined
- **WHEN** `content` is a list of `{"type": "text", "text": "..."}` blocks
- **THEN** texts joined with `""` (empty string)

#### Scenario: Last AI message wins
- **WHEN** multiple `event: values` events present
- **THEN** last event with `messages[-1].type == "ai"` determines content

#### Scenario: Heartbeats skipped
- **WHEN** stream contains `: heartbeat` lines
- **THEN** silently ignored

#### Scenario: Malformed JSON skipped
- **WHEN** `data:` line contains invalid JSON
- **THEN** warning emitted to stderr; parsing continues

#### Scenario: No AI message in stream
- **WHEN** the SSE stream contains no `event: values` event with `messages[-1].type == "ai"`
- **THEN** `ShadowbotQueryError` is raised with message "no AI response in stream"

### Requirement: ShadowbotResponse dataclass
`ShadowbotResponse` SHALL have: `content: str`, `thread_id: str | None`, and
`raw_event_count: int` property counting only successfully-parsed `data:` JSON
objects from `event: values` lines. Events of other types (e.g., `event: metadata`)
do NOT increment this counter.

#### Scenario: raw_event_count counts only values events
- **WHEN** stream contains one `event: metadata` event and two `event: values` events
- **THEN** `raw_event_count == 2`

### Requirement: State file security

The selected ShadowBot state file SHALL be written relative to the validated state-root directory descriptor. The client SHALL create a unique temporary file with exclusive creation, set its mode to `0o600` before writing, write and fsync complete JSON, then atomically replace the selected filename using source and destination directory descriptors. It SHALL close descriptors and remove any uncommitted temporary file on every outcome. This guarantees private permissions with no race window on both new creation and overwrite and prevents a replaced state-root pathname from redirecting persistence.

#### Scenario: State file created with correct permissions

- **WHEN** a thread ID is saved for the first time
- **THEN** the selected file is atomically created with mode `0o600`

#### Scenario: State file permissions enforced on overwrite

- **WHEN** the selected state file already exists with broader permissions
- **THEN** descriptor-relative atomic replacement leaves the final file at mode `0o600`

#### Scenario: State-root pathname replacement cannot redirect persistence

- **WHEN** the state-root pathname is replaced after target selection
- **THEN** persistence remains pinned to the originally opened directory descriptor

### Requirement: Session-scoped thread state

Before any remote request, the ShadowBot client SHALL select one state target beneath the validated ShadowBot state root using this precedence: `SHADOWBOT_STATE_FILE`, `SHADOWBOT_SESSION_ID`, `CLAUDE_SESSION_ID`, then `shadowbot-state.json`. Environment presence SHALL be determined by key membership, so a present empty or whitespace-only value is invalid and SHALL NOT fall through.

A relative explicit override SHALL be one direct-child filename. An absolute override SHALL have the validated state root as its parent. The root itself, nested or escaping paths, symlinks, and existing non-regular targets SHALL be rejected. Session-derived filenames SHALL be exactly `shadowbot-state-<session-id>.json`, where the ID is a full-string match of 1–128 alphanumeric, dash, or underscore characters. Invalid selectors SHALL raise `ShadowbotQueryError`.

#### Scenario: Selector precedence is deterministic

- **WHEN** all three environment selectors are present and valid
- **THEN** `SHADOWBOT_STATE_FILE` selects the target
- **WHEN** the explicit selector is absent and both session selectors are present
- **THEN** `SHADOWBOT_SESSION_ID` selects the target

#### Scenario: Session filenames are stable and isolated

- **WHEN** two callers use `alpha-session` and `beta-session`
- **THEN** their filenames are exactly `shadowbot-state-alpha-session.json` and `shadowbot-state-beta-session.json`

#### Scenario: Invalid higher-priority selector fails closed

- **WHEN** a higher-priority selector is present but empty, whitespace-only, contains a trailing newline, exceeds 128 characters, or names an invalid path
- **THEN** `ShadowbotQueryError` is raised without considering lower-priority selectors
- **THEN** no remote request occurs

#### Scenario: Explicit target validation

- **WHEN** the explicit value names the root, a nested path, traversal, an absolute path outside the root, a symlink, directory, FIFO, or other non-regular existing target
- **THEN** `ShadowbotQueryError` is raised before any remote request

#### Scenario: Global compatibility fallback

- **WHEN** none of the three selector keys exists in the environment
- **THEN** the selected filename is `shadowbot-state.json`

### Requirement: Descriptor-pinned state selection

The client SHALL create a missing validated state root with mode `0o700`, then open it without following symlinks and verify through the opened descriptor that it is a directory. It SHALL carry that directory descriptor plus the selected filename through the invocation. Reads and writes SHALL address the filename relative to that descriptor and SHALL not re-read selector environment variables.

Writes SHALL follow the modified State file security requirement.

#### Scenario: First-run state root is created privately

- **WHEN** the validated state root does not exist
- **THEN** the client creates it with mode `0o700`, opens it without following symlinks, and proceeds with the selected target

### Requirement: State failures map to query failures

State selection, root creation, root opening, descriptor validation, read, temporary creation, chmod, write, fsync, replace, cleanup, and descriptor-close failures SHALL raise `ShadowbotQueryError` with the original exception chained. A missing selected file SHALL mean no prior thread. Malformed JSON or an invalid stored thread ID SHALL continue to degrade to no prior thread.

#### Scenario: CLI reports a state failure

- **WHEN** target selection or persistence raises an underlying filesystem error
- **THEN** the CLI prints `Query failed:` with a safe state error
- **THEN** the CLI exits 1

### Requirement: New-thread semantics remain scoped

`--new` SHALL select and validate state before any remote request, skip loading an existing thread identifier, and persist the newly created identifier only through that selected target.

#### Scenario: Invalid new-thread selector has no remote effect

- **WHEN** `--new` is requested with an invalid selector
- **THEN** `ShadowbotQueryError` is raised before thread creation or query streaming
