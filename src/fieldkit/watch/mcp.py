"""Bounded MCP session client shared by optional watcher integrations."""

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

import httpx

from fieldkit.circuit_breaker import CircuitBreaker, CircuitOpenError
from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES, transient_retry
from fieldkit.errors import AuthError

log = logging.getLogger(__name__)

_MCP_TIMEOUT = 30.0  # seconds per complete HTTP response
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_BREAKER_LABEL = "optional-mcp"
_MCP_RESPONSE_MAX_BYTES = 1_048_576
_MCP_CONTENT_MAX_BLOCKS = 8
_MCP_TEXT_MAX_BYTES = 262_144
_MCP_CLEANUP_MAX_SECONDS = 0.1


class _DuplicateJSONKey(ValueError):
    """An ambiguous provider JSON object contained the same key twice."""


class _InvalidMCPResponse(ValueError):
    """The provider response exceeded or violated the bounded wire contract."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


class MCPAuthError(AuthError):
    """The configured MCP endpoint rejected authentication or authorization."""


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


async def _post_once(
    http: httpx.AsyncClient,
    url: str,
    content: bytes,
    headers: dict[str, str],
    response_timeout: float,
) -> tuple[httpx.Headers, bytes]:
    """POST, consume, and close one response within one absolute deadline.

    A small part of the caller's budget is reserved for cancellation-cooperative
    response cleanup. This uses only the maintained httpx async response API; it
    is not containment against an in-process custom transport that suppresses
    ``CancelledError``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + response_timeout
    cleanup_reserve = min(_MCP_CLEANUP_MAX_SECONDS, response_timeout / 2)
    response: httpx.Response | None = None
    operation_failed = False
    try:
        try:
            async with asyncio.timeout_at(deadline - cleanup_reserve):
                request = http.build_request(method="POST", url=url, content=content, headers=headers)
                response = await http.send(request, stream=True)
                response.raise_for_status()
                lengths = response.headers.get_list("content-length")
                if lengths:
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                        raise _InvalidMCPResponse
                    if int(lengths[0]) > _MCP_RESPONSE_MAX_BYTES:
                        raise _InvalidMCPResponse
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MCP_RESPONSE_MAX_BYTES:
                        raise _InvalidMCPResponse
                    body.extend(chunk)
                return httpx.Headers(response.headers), bytes(body)
        except TimeoutError:
            raise _InvalidMCPResponse from None
    except (httpx.HTTPError, _InvalidMCPResponse):
        operation_failed = True
        raise
    except Exception:  # noqa: BLE001 -- provider stream failures become a fixed diagnostic
        operation_failed = True
        raise _InvalidMCPResponse from None
    except BaseException:
        operation_failed = True
        raise
    finally:
        if response is not None:
            try:
                async with asyncio.timeout_at(deadline):
                    await response.aclose()
            except Exception:  # noqa: BLE001 -- cleanup cannot replace a known HTTP/auth failure
                if not operation_failed:
                    raise _InvalidMCPResponse from None


@transient_retry(_is_mcp_transient, log)
def _post_with_retry(
    runner: asyncio.Runner,
    http: httpx.AsyncClient,
    url: str,
    content: bytes,
    headers: dict[str, str],
    response_timeout: float,
) -> tuple[httpx.Headers, bytes]:
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
    return runner.run(_post_once(http, url, content, headers, response_timeout))


async def _new_http_client(timeout: float) -> httpx.AsyncClient:
    """Create the pooled async transport on the session's private event loop."""
    return httpx.AsyncClient(timeout=timeout)


async def _close_http_client(http: httpx.AsyncClient, timeout: float) -> None:
    """Best-effort cancellation-cooperative client cleanup within *timeout*."""
    try:
        async with asyncio.timeout(timeout):
            await http.aclose()
    except TimeoutError:
        return


def _validated_timeout(value: object) -> float:
    """Return a finite positive real timeout with ``bool`` rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("MCP timeout must be positive and finite")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP timeout must be positive and finite")
    return timeout


class MCPSession:
    """Stateful JSON-RPC 2.0 session against one configured MCP endpoint.

    Uses a pooled ``httpx.AsyncClient`` on a private event loop so synchronous
    watcher callers retain their API while response streaming remains cancellable.

    Supports the context manager protocol for guaranteed cleanup::

        with MCPSession(base_url) as session:
            result = session.call_tool(...)
    """

    def __init__(self, base_url: str, timeout: float = _MCP_TIMEOUT) -> None:
        timeout = _validated_timeout(timeout)
        self.base_url = base_url
        self.timeout = timeout
        self._session_id: str | None = None
        self._call_id = 0
        self._runner = asyncio.Runner()
        self._http = self._runner.run(_new_http_client(timeout))
        self._http_closed = False
        # implementation note: circuit breaker — opens after 3 consecutive call_tool failures,
        # resets after 60s. Initialization failures are not counted (they propagate
        # immediately as RuntimeError and are caught by the existing caller handlers).
        self._breaker = CircuitBreaker(name=_MCP_BREAKER_LABEL, failure_threshold=3, cooldown_seconds=60.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Perform the MCP initialize handshake and capture the session ID."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "morning-brief-watcher", "version": "1"},
            },
            "id": self._next_id(),
        }
        try:
            headers, body = self._post(payload, session_id=None)
            result = body.get("result")
            if "error" in body or not self._valid_initialize_result(result):
                raise RuntimeError("MCP initialization negotiation failed")
            session_ids = headers.get_list("mcp-session-id") if isinstance(headers, httpx.Headers) else []
            if not session_ids and isinstance(headers, Mapping):
                value = headers.get("mcp-session-id")
                session_ids = [value] if isinstance(value, str) else []
            if len(session_ids) != 1 or not self._valid_session_id(session_ids[0]):
                raise RuntimeError("MCP initialization negotiation failed")
            session_id = session_ids[0]
            self._session_id = session_id
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                session_id=session_id,
            )
        except BaseException:
            self._session_id = None
            with contextlib.suppress(BaseException):
                self._close_http()
            raise
        log.debug("MCP session established")

    def close(self) -> None:
        """Close the HTTP transport without sending a protocol shutdown message."""
        self._session_id = None
        self._close_http()

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
            result = self._breaker.call(self._call_tool_for_breaker, tool_name, arguments)
        except CircuitOpenError:
            raise RuntimeError("MCP service is temporarily unavailable; retry later") from None
        if isinstance(result, MCPAuthError):
            raise result
        return result

    def _call_tool_for_breaker(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Keep credential rejection outside the service-availability circuit.

        An authentication response proves the endpoint answered, so the breaker
        records a reachable service while the original authentication exception
        continues to the CLI's canonical exit-2 boundary.
        """
        try:
            return self._call_tool_inner(tool_name, arguments)
        except MCPAuthError as error:
            return error

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

        if "error" in body:
            raise RuntimeError("MCP tool request failed")

        if "result" not in body:
            raise RuntimeError("MCP tool returned an invalid result")
        result = body["result"]
        if not isinstance(result, dict):
            raise RuntimeError("MCP tool returned an invalid result")
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise RuntimeError("MCP tool returned an invalid result")
        if is_error:
            raise RuntimeError("MCP tool request failed (isError=true)")

        content = result.get("content", [])
        if not isinstance(content, list):
            raise RuntimeError("MCP tool returned invalid content")
        if len(content) > _MCP_CONTENT_MAX_BLOCKS:
            raise RuntimeError("MCP tool returned invalid content")
        if "structuredContent" in result:
            structured = result["structuredContent"]
            if content or not isinstance(structured, dict):
                raise RuntimeError("MCP tool returned invalid content")
            return structured
        if not content:
            raise RuntimeError("MCP tool returned no usable text content")
        if len(content) != 1:
            raise RuntimeError("MCP tool returned invalid content")
        item = content[0]
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            raise RuntimeError("MCP tool returned no usable text content")
        raw = item["text"]
        if not raw.strip() or len(raw.encode("utf-8")) > _MCP_TEXT_MAX_BYTES:
            raise RuntimeError("MCP tool returned invalid content")
        try:
            parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
        except _DuplicateJSONKey:
            raise RuntimeError("MCP tool returned invalid content") from None
        except json.JSONDecodeError:
            return raw
        except (RecursionError, ValueError):
            raise RuntimeError("MCP tool returned invalid content") from None
        if parsed is None:
            raise RuntimeError("MCP tool returned no usable text content")
        return parsed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    @staticmethod
    def _valid_initialize_result(result: object) -> bool:
        if not isinstance(result, dict) or result.get("protocolVersion") != _MCP_PROTOCOL_VERSION:
            return False
        capabilities = result.get("capabilities")
        server_info = result.get("serverInfo")
        return (
            isinstance(capabilities, dict)
            and isinstance(capabilities.get("tools"), dict)
            and isinstance(server_info, dict)
            and isinstance(server_info.get("name"), str)
            and bool(server_info["name"].strip())
            and isinstance(server_info.get("version"), str)
            and bool(server_info["version"].strip())
        )

    @staticmethod
    def _valid_session_id(value: str) -> bool:
        return 0 < len(value) <= 1024 and all(0x21 <= ord(character) <= 0x7E for character in value)

    def _close_http(self) -> None:
        """Best-effort cleanup that cannot mask the triggering provider failure."""
        if self._http_closed:
            return
        try:
            cleanup_timeout = min(_MCP_CLEANUP_MAX_SECONDS, self.timeout)
            self._runner.run(_close_http_client(self._http, cleanup_timeout))
        except Exception:  # noqa: BLE001 -- teardown must preserve the original provider/auth failure
            log.debug("MCP HTTP transport cleanup failed")
        finally:
            self._http_closed = True
            self._runner.close()

    def _post(
        self, payload: dict[str, Any], session_id: str | None
    ) -> tuple[httpx.Headers | dict[str, str], dict[str, Any]]:
        """POST JSON-RPC payload; return (response_headers, parsed_body).

        The private event loop keeps one ``AsyncClient`` pooled across calls while
        ``asyncio.timeout`` supplies a cancellable whole-response deadline.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Mcp-Session-Id": session_id} if session_id else {}),
        }
        try:
            raw_headers, raw_bytes = _post_with_retry(
                self._runner,
                self._http,
                self.base_url,
                json.dumps(payload).encode(),
                headers,
                float(self.timeout),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise MCPAuthError("MCP authentication failed; refresh the configured service credentials") from None
            raise RuntimeError("MCP request failed; retry later") from None
        except httpx.HTTPError:
            raise RuntimeError("MCP request failed; retry later") from None
        except _InvalidMCPResponse:
            raise RuntimeError("MCP endpoint returned an invalid response") from None

        try:
            raw_body = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("MCP endpoint returned an invalid response") from None

        request_id = payload.get("id")
        if not raw_body.strip():
            if request_id is None:
                return raw_headers, {}
            raise RuntimeError("MCP endpoint returned an empty response")

        try:
            body = json.loads(raw_body, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, _DuplicateJSONKey, RecursionError, ValueError):
            raise RuntimeError("MCP endpoint returned an invalid response") from None
        if not isinstance(body, dict):
            raise RuntimeError("MCP endpoint returned an invalid response")

        if request_id is None:
            raise RuntimeError("MCP endpoint returned an unexpected response to a notification")
        if body.get("jsonrpc") != "2.0":
            raise RuntimeError("MCP endpoint returned an invalid JSON-RPC version")
        response_id = body.get("id")
        if type(response_id) is not type(request_id) or response_id != request_id:
            raise RuntimeError("MCP endpoint returned a mismatched response id")
        has_result = "result" in body
        has_error = "error" in body
        if has_result == has_error:
            raise RuntimeError("MCP endpoint response must contain exactly one result or error")
        if has_error:
            error = body["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), int)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("message"), str)
            ):
                raise RuntimeError("MCP endpoint returned an invalid error response")

        return raw_headers, body
