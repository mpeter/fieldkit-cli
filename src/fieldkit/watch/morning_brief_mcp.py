"""MCP session client for the morning brief watcher — domain module.

Moved from ``commands/watch/morning_brief_mcp.py`` (watch-domain-migration,
implementation change slice 2.5). No Click imports — pure MCP session infrastructure.

Provides a stateful JSON-RPC 2.0 session against a single mcpjungle group
endpoint. Extracted from morning_brief.py to keep the main module focused
on orchestration.

Note: ``_MCP_CALENDAR_BASE`` is computed from ``get_mcp_gateway_base()`` at
import time (frozen constant). Tests must patch ``get_mcp_gateway_base``
before importing this module, or patch ``_MCP_CALENDAR_BASE`` directly.
"""

import json
import logging
from typing import Any

import httpx

from fieldkit.circuit_breaker import CircuitBreaker, CircuitOpenError
from fieldkit.config import get_mcp_gateway_base as _get_mcp_gateway_base
from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES, transient_retry

log = logging.getLogger(__name__)

_MCP_CALENDAR_BASE = f"{_get_mcp_gateway_base()}/v0/groups/fieldkit-calendar/mcp"
_MCP_TIMEOUT = 30  # seconds per HTTP call


def _is_mcp_transient(exc: BaseException) -> bool:
    """True for retryable MCP gateway failures.

    Transport failures (no response arrived) plus the shared transient status set.
    A 4xx is deliberately excluded: the gateway understood the request and rejected
    it, so repeating it cannot help. The original historic regression fix matched
    ``httpx.HTTPError``, which is the base of both ``RequestError`` and
    ``HTTPStatusError`` — so it retried 401/403/404 as well, three times, before
    surfacing an error that was never going to change.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_TRANSIENT_STATUSES
    return isinstance(exc, httpx.RequestError)


@transient_retry(_is_mcp_transient, log)
def _post_with_retry(http: httpx.Client, url: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
    """POST a JSON-RPC payload to the MCP gateway, retrying transient failures.

    ``transient_retry`` (not ``connect_retry``) because every JSON-RPC method routed
    through here is a read: ``initialize``, ``notifications/initialized``, and
    ``tools/call`` for the four tools this codebase invokes — ``backstory__find_account``,
    ``backstory__get_account_status``, ``google_workspace__search_gmail_messages``,
    ``google_workspace__get_events``. Repeating any of them is harmless.

    That is an assumption about the *tools*, not about JSON-RPC: ``call_tool()`` takes
    an arbitrary tool name, so a mutating tool added later would inherit this policy
    and be re-run on a 5xx. ``test_mcp_tool_allowlist_is_reviewed`` fails if a new tool
    name appears, forcing that judgement to be made rather than inherited.

    Module-level so the decorator does not bind to self. Raises the raw httpx error
    after retries are exhausted; the caller (MCPSession._post) converts it.
    """
    resp = http.post(url, content=content, headers=headers)
    resp.raise_for_status()
    return resp


class MCPSession:
    """Stateful JSON-RPC 2.0 session against a single mcpjungle group endpoint.

    implementation note: Uses httpx.Client for connection pooling (reuses the TCP connection
    across multiple MCP calls, reducing per-call overhead vs urllib).

    Supports the context manager protocol for guaranteed cleanup::

        with MCPSession(base_url) as session:
            result = session.call_tool(...)
    """

    def __init__(self, base_url: str, timeout: int = _MCP_TIMEOUT) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._session_id: str | None = None
        self._call_id = 0
        self._http = httpx.Client(timeout=float(timeout))
        # implementation note: circuit breaker — opens after 3 consecutive call_tool failures,
        # resets after 60s. Initialization failures are not counted (they propagate
        # immediately as RuntimeError and are caught by the existing caller handlers).
        self._breaker = CircuitBreaker(name=base_url, failure_threshold=3, cooldown_seconds=60.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Perform the MCP initialize handshake and capture the session ID."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "morning-brief-watcher", "version": "1"},
            },
            "id": self._next_id(),
        }
        headers, _body = self._post(payload, session_id=None)
        session_id = headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError(
                "MCP initialize did not return Mcp-Session-Id header. Verify the configured MCP gateway endpoint."
            )
        self._session_id = session_id
        log.debug("MCP session established: %s", session_id)

    def close(self) -> None:
        """Best-effort session teardown (errors silently swallowed)."""
        if self._session_id:
            try:
                payload: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
                self._post(payload, session_id=self._session_id)
            except Exception:  # noqa: BLE001  # MCP notifications/initialized is best-effort on teardown
                log.debug("MCP notifications/initialized on close failed — ignored", exc_info=True)
            self._session_id = None
        self._http.close()

    def __enter__(self) -> "MCPSession":
        """Context manager entry — calls initialize() and returns self."""
        self.initialize()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit — calls close() unconditionally. Does not suppress exceptions."""
        self.close()

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool through the circuit breaker.

        implementation note: wraps _call_tool_inner with self._breaker so that 3 consecutive
        failures open the circuit and subsequent calls fail fast with RuntimeError.
        """
        try:
            return self._breaker.call(self._call_tool_inner, tool_name, arguments)
        except CircuitOpenError as exc:
            raise RuntimeError(f"MCP circuit open for {self.base_url} — service unavailable: {exc}") from None

    def _call_tool_inner(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a single MCP tool call (called through the circuit breaker).

        Returns the first text block parsed as JSON if possible, otherwise
        as a raw string. Raises RuntimeError on MCP-level errors.
        """
        if not self._session_id:
            raise RuntimeError("MCPSession.initialize() must be called first.")
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
            "id": self._next_id(),
        }
        _headers, body = self._post(payload, session_id=self._session_id)

        error = body.get("error")
        if error:
            raise RuntimeError(f"MCP tool '{tool_name}' returned error: {error}")

        result = body.get("result", {})
        if result.get("isError"):
            content_texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
            raise RuntimeError(f"MCP tool '{tool_name}' isError=true: {' | '.join(content_texts)}")

        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                raw = item["text"]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    def _post(self, payload: dict[str, Any], session_id: str | None) -> tuple[dict[str, str], dict[str, Any]]:
        """POST JSON-RPC payload; return (response_headers, parsed_body).

        implementation note: Uses self._http (httpx.Client) for connection pooling.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Mcp-Session-Id": session_id} if session_id else {}),
        }
        try:
            resp = _post_with_retry(self._http, self.base_url, json.dumps(payload).encode(), headers)
            raw_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw_body = resp.text
        except httpx.HTTPError as exc:
            raise RuntimeError(f"MCP HTTP error for {self.base_url}: {exc}") from exc

        # Guard: MCP server may close the connection without a body during teardown
        # (e.g. notifications/initialized on close). Return empty dicts rather than
        # letting json.loads raise JSONDecodeError → RuntimeError with a traceback.
        # D3 in design.md: both callers handle ({}, {}) gracefully.
        if not raw_body.strip():
            return {}, {}

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MCP non-JSON response from {self.base_url}: {raw_body[:200]!r}") from exc

        return raw_headers, body
