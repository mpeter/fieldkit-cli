"""fieldkit shadowbot client — LangGraph Server streaming client.

Targets the configured ShadowBot API base (``shadowbot.api_base`` in config.yaml).
Creates threads via ``POST /threads``, streams responses via
``POST /threads/{id}/runs/stream``, and parses the SSE response to extract
the final AI message content.

Thread ID is persisted under the state directory and reused on subsequent calls.
Session selectors isolate concurrent callers; ``--new`` creates a fresh thread.

Public API:
  query(prompt, token, timeout, new_thread) -> ShadowbotResponse  Convenience wrapper.
  ShadowbotClient(token)                                           Stateful client class.
  ShadowbotResponse                                                Typed response dataclass.
  ShadowbotQueryError                                              Raised on non-auth errors.
"""

import json
import logging
import math
import re
import time
import warnings
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any

import httpx

from fieldkit.config import TIMEOUT_SHADOWBOT_QUERY, get_shadowbot_api_base
from fieldkit.config.retry import RETRY_MAX_ATTEMPTS, RETRY_WAIT_MAX, RETRY_WAIT_MIN
from fieldkit.errors import FieldkitError
from fieldkit.shadowbot.auth import ShadowbotAuthError
from fieldkit.shadowbot.state import (
    ShadowbotStateError,
    StateTarget,
    load_thread_id,
    save_thread_id,
    state_target,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# implementation note: get_shadowbot_api_base() and _ASSISTANT_ID removed — now read from config via:
#   get_shadowbot_api_base()     (was get_shadowbot_api_base())
#   get_shadowbot_assistant_id() (was _ASSISTANT_ID, already migrated before this spec)
_monotonic = time.monotonic

# Thread ID validation: UUID or safe alphanumeric/dash/underscore
_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    r"|^[a-zA-Z0-9_-]+$"
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ShadowbotQueryError(FieldkitError):
    """Raised on non-auth HTTP errors, timeouts, or connection failures."""


@contextmanager
def _query_state_target() -> Iterator[StateTarget]:
    """Translate state-boundary failures into the public query error."""
    try:
        with state_target() as target:
            yield target
    except ShadowbotStateError as exc:
        raise ShadowbotQueryError(str(exc)) from (exc.__cause__ or exc)


def _remaining_timeout(deadline: float, total_timeout: float) -> float:
    """Return the remaining query budget or raise when its deadline expired."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise ShadowbotQueryError(f"ShadowBot query exceeded total timeout of {total_timeout:.0f}s")
    return remaining


def _validate_timeout(timeout: float) -> None:
    """Reject timeout values that cannot define a finite deadline."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ShadowbotQueryError("Timeout must be a finite positive number of seconds.")


def _lines_until_deadline(response: httpx.Response, deadline: float, total_timeout: float) -> Iterable[str]:
    """Yield SSE lines while enforcing the caller-visible query deadline."""
    items: Queue[str | httpx.HTTPError | None] = Queue(maxsize=1)
    stopped = Event()

    def enqueue(item: str | httpx.HTTPError | None) -> bool:
        while not stopped.is_set():
            try:
                items.put(item, timeout=0.05)
            except Full:
                continue
            return True
        return False

    def read_lines() -> None:
        try:
            for line in response.iter_lines():
                if not enqueue(line):
                    return
        except httpx.HTTPError as error:
            enqueue(error)
        finally:
            enqueue(None)

    reader = Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        while True:
            try:
                item = items.get(timeout=_remaining_timeout(deadline, total_timeout))
            except Empty as error:
                raise ShadowbotQueryError(f"ShadowBot query exceeded total timeout of {total_timeout:.0f}s") from error
            if item is None:
                return
            if isinstance(item, str):
                yield item
                continue
            raise item
    finally:
        stopped.set()
        response.close()
        reader.join(timeout=0.1)


def _call_until_deadline(
    client: httpx.Client,
    operation: Callable[[], httpx.Response],
    *,
    deadline: float,
    total_timeout: float,
) -> httpx.Response:
    """Run blocking HTTP work in a cancellable worker through ``deadline``."""
    _remaining_timeout(deadline, total_timeout)
    items: Queue[httpx.Response | Exception] = Queue(maxsize=1)
    stopped = Event()

    def enqueue(item: httpx.Response | Exception) -> None:
        while not stopped.is_set():
            try:
                items.put(item, timeout=0.05)
            except Full:
                continue
            return

    def send() -> None:
        try:
            _remaining_timeout(deadline, total_timeout)
            enqueue(operation())
        except (httpx.HTTPError, ShadowbotQueryError) as error:
            enqueue(error)

    worker = Thread(target=send, daemon=True)
    worker.start()
    try:
        try:
            item = items.get(timeout=_remaining_timeout(deadline, total_timeout))
        except Empty as error:
            client.close()
            raise ShadowbotQueryError(f"ShadowBot query exceeded total timeout of {total_timeout:.0f}s") from error
        if isinstance(item, Exception):
            raise item
        return item
    finally:
        stopped.set()
        worker.join(timeout=0.1)


def _send_create_thread_until_deadline(
    client: httpx.Client,
    request: httpx.Request,
    *,
    deadline: float,
    total_timeout: float,
) -> httpx.Response:
    """Retry only safe thread-create connection failures within the query deadline."""
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            return _call_until_deadline(
                client,
                lambda: client.send(request),
                deadline=deadline,
                total_timeout=total_timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if attempt + 1 == RETRY_MAX_ATTEMPTS:
                logger.error("Thread creation failed after %d attempts: %s", RETRY_MAX_ATTEMPTS, error)
                error.add_note(f"Thread creation failed after {RETRY_MAX_ATTEMPTS} attempts")
                raise

            backoff = min(RETRY_WAIT_MAX, max(RETRY_WAIT_MIN, 2**attempt))
            if _remaining_timeout(deadline, total_timeout) <= backoff:
                raise ShadowbotQueryError(f"ShadowBot query exceeded total timeout of {total_timeout:.0f}s") from error
            logger.warning("Thread creation failed; retrying in %.1fs", backoff)
            time.sleep(backoff)

    raise AssertionError("thread creation retry loop ended unexpectedly")  # pragma: no cover


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass
class ShadowbotResponse:
    """Parsed response from the ShadowBot LangGraph streaming API."""

    content: str
    thread_id: str | None = None
    _raw_events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def raw_event_count(self) -> int:
        """Number of successfully-parsed ``event: values`` JSON objects."""
        return len(self._raw_events)


# ---------------------------------------------------------------------------
# Pure SSE parser
# ---------------------------------------------------------------------------


def _extract_ai_content(last_msg: dict[str, Any]) -> str | None:
    """Extract text content from an AI message dict.

    Returns the content string, or None if the message is not an AI message.
    Handles both plain string content and list-of-text-block content.
    """
    if last_msg.get("type") != "ai":
        return None
    content = last_msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return None


def _process_sse_data_line(
    payload: str,
    raw_events: list[dict[str, Any]],
) -> str | None:
    """Parse one SSE data payload and return AI content if found, else None.

    Appends the parsed event to *raw_events* in place.
    Emits a warning on malformed JSON and returns None.
    """
    try:
        event: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError as exc:
        warnings.warn(f"malformed JSON in SSE stream: {exc}", stacklevel=3)
        return None

    raw_events.append(event)

    messages = event.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    last_msg = messages[-1]
    if not isinstance(last_msg, dict):
        return None

    return _extract_ai_content(last_msg)


def _parse_sse_stream(lines: Iterable[str]) -> ShadowbotResponse:
    """Parse a LangGraph Server-Sent Events stream.

    Accumulates only ``event: values`` events (skips ``event: metadata`` and
    others). The last ``event: values`` event where
    ``messages[-1]["type"] == "ai"`` determines the response content.

    String content is returned as-is. List content (``{"type": "text", "text": "..."}``
    blocks) is joined with ``""`` (empty string).

    Heartbeat lines (``: heartbeat``) are silently skipped.
    Malformed JSON emits a ``warnings.warn`` and continues.

    Args:
        lines: Iterable of raw SSE text lines.

    Returns:
        A populated ``ShadowbotResponse``.

    Raises:
        ShadowbotQueryError: If no ``event: values`` event contains an AI message.
    """
    raw_events: list[dict[str, Any]] = []
    final_content: str | None = None
    current_event_type: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")

        if line.startswith(":"):
            continue  # heartbeat — skip

        if line.startswith("event:"):
            current_event_type = line[len("event:") :].strip()
            continue

        if not line:
            current_event_type = None
            continue

        if not line.startswith("data:"):
            continue

        payload = line[len("data:") :].lstrip(" ")

        if current_event_type != "values":
            continue

        content = _process_sse_data_line(payload, raw_events)
        if content is not None:
            final_content = content

    if final_content is None:
        raise ShadowbotQueryError("no AI response in stream")

    return ShadowbotResponse(content=final_content, _raw_events=raw_events)


# ---------------------------------------------------------------------------
# ShadowbotClient
# ---------------------------------------------------------------------------


class ShadowbotClient:
    """LangGraph Server client for the ShadowBot sales assistant.

    Persists the thread_id between calls so follow-up questions share context.
    Pass ``new_thread=True`` to start a fresh thread.

    Args:
        token: A valid ShadowBot JWT (without the ``Bearer `` prefix).
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def query(
        self,
        prompt: str,
        timeout: float = TIMEOUT_SHADOWBOT_QUERY,
        new_thread: bool = False,
    ) -> ShadowbotResponse:
        """Send a prompt to ShadowBot and return the parsed streaming response.

        Args:
            prompt: The natural-language query.
            timeout: Total query deadline in seconds (default 300 s).
            new_thread: If True, create a new thread regardless of persisted state.

        Returns:
            A populated ``ShadowbotResponse``.

        Raises:
            ShadowbotAuthError: On HTTP 401.
            ShadowbotQueryError: On other HTTP errors, timeouts, or connection failures.
        """
        _validate_timeout(timeout)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        deadline = _monotonic() + timeout
        return self._query_with_state(
            headers=headers,
            deadline=deadline,
            total_timeout=timeout,
            prompt=prompt,
            new_thread=new_thread,
        )

    def _query_with_state(
        self,
        *,
        headers: dict[str, str],
        deadline: float,
        total_timeout: float,
        prompt: str,
        new_thread: bool,
    ) -> ShadowbotResponse:
        with _query_state_target() as target, httpx.Client(timeout=httpx.Timeout(total_timeout)) as client:
            return self._query_with_stale_thread_recovery(
                client=client,
                headers=headers,
                deadline=deadline,
                total_timeout=total_timeout,
                prompt=prompt,
                new_thread=new_thread,
                state_target=target,
            )

    def _query_with_stale_thread_recovery(
        self,
        *,
        client: httpx.Client,
        headers: dict[str, str],
        deadline: float,
        total_timeout: float,
        prompt: str,
        new_thread: bool,
        state_target: StateTarget,
    ) -> ShadowbotResponse:
        thread_id: str | None = None if new_thread else load_thread_id(state_target)
        if thread_id is None:
            thread_id = self._create_thread(client, headers, deadline, total_timeout)

        retried_stale = False
        while True:
            response = self._stream_query(
                client=client,
                thread_id=thread_id,
                prompt=prompt,
                headers=headers,
                deadline=deadline,
                total_timeout=total_timeout,
            )
            if response is None:
                if retried_stale:
                    raise ShadowbotQueryError(
                        "Thread not found after recovery attempt. Try: fieldkit shadowbot query --new"
                    )
                retried_stale = True
                thread_id = self._create_thread(client, headers, deadline, total_timeout)
                continue

            response.thread_id = thread_id
            save_thread_id(state_target, thread_id)
            return response

    def _create_thread(
        self, client: httpx.Client, headers: dict[str, str], deadline: float, total_timeout: float
    ) -> str:
        """Create a new LangGraph thread and return its ID.

        Args:
            client: The query-scoped HTTP client, closed if its deadline expires.
            headers: HTTP headers including Authorization.
            deadline: Absolute monotonic query deadline.
            total_timeout: Original total query timeout for error reporting.

        Returns:
            The new thread_id string.

        Raises:
            ShadowbotAuthError: On HTTP 401.
            ShadowbotQueryError: On other non-200 responses or invalid thread_id format.
        """
        try:
            request = client.build_request("POST", f"{get_shadowbot_api_base()}/threads", json={}, headers=headers)
            resp = _send_create_thread_until_deadline(
                client,
                request,
                deadline=deadline,
                total_timeout=total_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ShadowbotQueryError(f"Timed out creating ShadowBot thread ({total_timeout:.0f}s): {exc}") from exc
        except httpx.RequestError as exc:
            raise ShadowbotQueryError(f"Connection error creating ShadowBot thread: {exc}") from exc

        try:
            if resp.status_code == 401:
                raise ShadowbotAuthError(
                    "ShadowBot authentication failed (HTTP 401) on thread creation. "
                    "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
                )
            if resp.status_code != 200:
                raise ShadowbotQueryError(
                    f"Failed to create ShadowBot thread (HTTP {resp.status_code}): {resp.text[:200]}"
                )

            try:
                data: dict[str, Any] = resp.json()
            except Exception as exc:
                raise ShadowbotQueryError(f"Invalid response from thread creation: {exc}") from exc

            thread_id = str(data.get("thread_id", ""))
            if not thread_id:
                raise ShadowbotQueryError("Thread creation response missing thread_id field.")

            # Validate thread_id format before persisting (prevents path traversal)
            if not _THREAD_ID_RE.match(thread_id):
                raise ShadowbotQueryError(
                    f"Thread ID has invalid format: {thread_id!r}. Expected UUID or alphanumeric/dash/underscore."
                )

            return thread_id
        finally:
            resp.close()

    def _stream_query(
        self,
        *,
        client: httpx.Client,
        thread_id: str,
        prompt: str,
        headers: dict[str, str],
        deadline: float,
        total_timeout: float,
    ) -> ShadowbotResponse | None:
        """POST to the stream endpoint and parse the SSE response.

        Returns:
            Parsed ``ShadowbotResponse`` on success, or ``None`` if the
            thread returned 404 (stale thread — caller should recover).

        Raises:
            ShadowbotAuthError: On HTTP 401.
            ShadowbotQueryError: On other non-200 responses, timeouts,
                non-SSE content type, or parse errors.
        """
        from fieldkit.config import get_shadowbot_assistant_id

        stream_url = f"{get_shadowbot_api_base()}/threads/{thread_id}/runs/stream"
        payload = {
            # implementation change: read assistant_id from config at query time so operators
            # can override via shadowbot.assistant_id in config.yaml without rebuild.
            "assistant_id": get_shadowbot_assistant_id(),
            "input": {"messages": [{"role": "human", "content": prompt}]},
            "stream_mode": ["values"],
        }
        try:
            request = client.build_request("POST", stream_url, json=payload, headers=headers)
            resp = _call_until_deadline(
                client,
                lambda: client.send(request, stream=True),
                deadline=deadline,
                total_timeout=total_timeout,
            )
            try:
                if resp.status_code == 401:
                    raise ShadowbotAuthError(
                        "ShadowBot authentication failed (HTTP 401) during streaming. "
                        "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
                    )
                if resp.status_code == 404:
                    return None  # stale thread — signal caller to recover

                if resp.status_code != 200:
                    raise ShadowbotQueryError(f"ShadowBot stream failed (HTTP {resp.status_code}): {resp.text[:200]}")

                # Verify Content-Type before parsing (D8)
                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    raise ShadowbotQueryError(
                        f"Expected text/event-stream response but got: {content_type!r}. "
                        "The API may have returned an error page."
                    )

                return _parse_sse_stream(_lines_until_deadline(resp, deadline, total_timeout))
            finally:
                resp.close()

        except (ShadowbotAuthError, ShadowbotQueryError):
            raise
        except httpx.TimeoutException as exc:
            raise ShadowbotQueryError(f"ShadowBot query timed out after {total_timeout:.0f}s: {exc}") from exc
        except httpx.RequestError as exc:
            raise ShadowbotQueryError(f"ShadowBot connection failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------


def query(
    prompt: str,
    token: str,
    *,
    timeout: float = TIMEOUT_SHADOWBOT_QUERY,
    new_thread: bool = False,
) -> ShadowbotResponse:
    """Send a prompt to ShadowBot and return the parsed response.

    Convenience wrapper around ``ShadowbotClient.query()``.

    Args:
        prompt: The natural-language query.
        token: A valid ShadowBot JWT (without the ``Bearer `` prefix).
        timeout: Total query deadline in seconds (default 300 s).
        new_thread: Start a fresh thread (don't reuse persisted thread_id).

    Returns:
        A populated ``ShadowbotResponse``.

    Raises:
        ShadowbotAuthError: On HTTP 401.
        ShadowbotQueryError: On other errors.
    """
    return ShadowbotClient(token).query(prompt, timeout=timeout, new_thread=new_thread)
