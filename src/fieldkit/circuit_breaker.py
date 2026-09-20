"""Circuit breaker pattern for external service calls (implementation note).

Provides a three-state circuit breaker (CLOSED → OPEN → HALF-OPEN → CLOSED)
to fail fast when an external service is consistently unavailable.

Placement: top-level package module with zero dependencies — importable by
any domain (SF, Gmail, LLM, ShadowBot, MCP) without tach boundary violations.

Future: if CircuitOpenError needs to be caught outside the module that creates
the CircuitBreaker, move it to fieldkit.errors.
"""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open and a call is rejected fast.

    This is an implementation detail of CircuitBreaker — callers that need to
    catch it should be in the same module as the CircuitBreaker instance.
    External callers see only RuntimeError (re-raised by the using module).
    """


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF-OPEN → CLOSED.

    States:
        CLOSED:    Requests pass through normally.
        OPEN:      Requests are rejected immediately (CircuitOpenError) without
                   calling the wrapped function. Entered after ``failure_threshold``
                   consecutive failures.
        HALF-OPEN: One probe request is allowed through after ``cooldown_seconds``.
                   Success → CLOSED; failure → OPEN again.

    Example::

        breaker = CircuitBreaker(name="mcp-calendar", failure_threshold=3)

        try:
            result = breaker.call(make_mcp_request, payload)
        except CircuitOpenError:
            # Service unavailable — fail fast
            raise RuntimeError("MCP circuit open") from None
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        """Initialise a circuit breaker.

        Args:
            name:              Human-readable label for log messages.
            failure_threshold: Consecutive failures before the circuit opens.
            cooldown_seconds:  Seconds to wait before allowing a probe in HALF-OPEN.
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        """Current circuit state: 'closed', 'open', or 'half-open'."""
        return self._state

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call *fn* through the circuit breaker.

        Args:
            fn:      Callable to invoke.
            *args:   Positional arguments for *fn*.
            **kwargs: Keyword arguments for *fn*.

        Returns:
            The return value of *fn*.

        Raises:
            CircuitOpenError: If the circuit is open and the cooldown has not elapsed.
            Any exception raised by *fn*: re-raised after updating failure count.
        """
        if self._state == self.OPEN:
            elapsed = time.monotonic() - (self._opened_at or 0.0)
            if elapsed >= self.cooldown_seconds:
                log.info("circuit-breaker[%s]: half-open — allowing probe", self.name)
                self._state = self.HALF_OPEN
            else:
                raise CircuitOpenError(
                    f"Circuit breaker '{self.name}' is open (cooldown {self.cooldown_seconds - elapsed:.0f}s remaining)"
                )

        try:
            result: T = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        if self._state != self.CLOSED:
            log.info("circuit-breaker[%s]: closed after successful probe", self.name)
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = None

    def _on_failure(self) -> None:
        self._failures += 1
        if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()
            log.warning(
                "circuit-breaker[%s]: opened after %d failure(s)",
                self.name,
                self._failures,
            )
