"""Unit tests for CircuitBreaker in fieldkit/circuit_breaker.py (implementation note)."""

import time

import pytest

from fieldkit.circuit_breaker import CircuitBreaker, CircuitOpenError

pytestmark = pytest.mark.unit


def _make_breaker(threshold: int = 3, cooldown: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker(name="test-service", failure_threshold=threshold, cooldown_seconds=cooldown)


# ---------------------------------------------------------------------------
# CLOSED → OPEN after N failures
# ---------------------------------------------------------------------------


def test_closed_to_open_after_threshold_failures() -> None:
    breaker = _make_breaker(threshold=3)

    def _fail() -> None:
        raise RuntimeError("service down")

    for _ in range(3):
        with pytest.raises(RuntimeError, match="service down"):
            breaker.call(_fail)

    assert breaker.state == CircuitBreaker.OPEN


def test_open_raises_circuit_open_error_immediately() -> None:
    breaker = _make_breaker(threshold=1, cooldown=999.0)

    def _fail() -> None:
        raise RuntimeError("service down")

    with pytest.raises(RuntimeError, match="service down"):
        breaker.call(_fail)

    assert breaker.state == CircuitBreaker.OPEN

    # Next call raises CircuitOpenError without calling the function
    call_count = 0

    def _should_not_be_called() -> str:
        nonlocal call_count
        call_count += 1
        return "ok"

    with pytest.raises(CircuitOpenError, match="open"):
        breaker.call(_should_not_be_called)

    assert call_count == 0, "Function should not be called when circuit is open"


# ---------------------------------------------------------------------------
# OPEN → HALF-OPEN after cooldown
# ---------------------------------------------------------------------------


def test_open_to_half_open_after_cooldown() -> None:
    breaker = _make_breaker(threshold=1, cooldown=60.0)

    def _fail() -> None:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        breaker.call(_fail)

    assert breaker.state == CircuitBreaker.OPEN

    # Simulate cooldown elapsed by backdating opened_at past the window.
    # time.monotonic() is uptime-based: on a freshly booted CI runner it can be
    # smaller than the cooldown, so `_opened_at = 0.0` does NOT guarantee
    # elapsed > cooldown (observed: runner uptime 59s vs cooldown 60s).
    probe_called = False

    def _probe() -> str:
        nonlocal probe_called
        probe_called = True
        return "ok"

    breaker._opened_at = time.monotonic() - 61.0
    result = breaker.call(_probe)
    assert result == "ok"
    assert probe_called is True
    assert breaker.state == CircuitBreaker.CLOSED


# ---------------------------------------------------------------------------
# HALF-OPEN failure → OPEN again
# ---------------------------------------------------------------------------


def test_half_open_failure_reopens_circuit() -> None:
    breaker = _make_breaker(threshold=1, cooldown=60.0)

    def _fail() -> None:
        raise RuntimeError("still down")

    with pytest.raises(RuntimeError, match="still down"):
        breaker.call(_fail)

    # Force into HALF-OPEN
    breaker._state = CircuitBreaker.HALF_OPEN
    breaker._opened_at = 0.0

    with pytest.raises(RuntimeError, match="still down"):
        breaker.call(_fail)

    assert breaker.state == CircuitBreaker.OPEN


# ---------------------------------------------------------------------------
# Success resets the circuit
# ---------------------------------------------------------------------------


def test_success_resets_failure_count() -> None:
    breaker = _make_breaker(threshold=3)

    def _fail() -> None:
        raise RuntimeError("fail")

    def _succeed() -> str:
        return "ok"

    # 2 failures
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fail"):
            breaker.call(_fail)

    assert breaker.state == CircuitBreaker.CLOSED
    assert breaker._failures == 2

    # Success resets
    result = breaker.call(_succeed)
    assert result == "ok"
    assert breaker._failures == 0
    assert breaker.state == CircuitBreaker.CLOSED
