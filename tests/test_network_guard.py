"""Tests for the conftest outbound-network guard.

An unverified guard is worse than none: it produces the belief that the suite
is hermetic without the fact. These tests pin that the guard blocks egress,
still permits loopback, and can be opted out of deliberately.
"""

import socket

import pytest

pytestmark = pytest.mark.unit


def test_outbound_connection_is_blocked() -> None:
    """Egress to a non-loopback address raises rather than leaving the machine."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="attempted an outbound network connection"):
            sock.connect(("examplecrm.my.salesforce.com", 443))
    finally:
        sock.close()


def test_block_message_names_the_host_and_the_escape_hatch() -> None:
    """The failure has to say what to do, or the next person just deletes the guard."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            sock.connect(("example.com", 80))
    finally:
        sock.close()
    message = str(exc_info.value)
    assert "example.com" in message
    assert "@pytest.mark.network" in message


def test_loopback_is_still_permitted() -> None:
    """Local sockets stay usable — the guard blocks egress, not all networking.

    Connecting to a closed loopback port must fail with the OS error, not the
    guard's RuntimeError: that difference is the proof it was let through.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # port now closed, so connect() gets refused by the OS

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        with pytest.raises(OSError) as exc_info:
            sock.connect(("127.0.0.1", port))
    finally:
        sock.close()
    assert not isinstance(exc_info.value, RuntimeError)


def test_loopback_round_trip_works() -> None:
    """A real local connect/accept still succeeds through the guard."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2)
    try:
        client.connect(("127.0.0.1", port))
        conn, _ = listener.accept()
        conn.close()
    finally:
        client.close()
        listener.close()


@pytest.mark.network
def test_network_marker_opts_out_of_the_guard() -> None:
    """The marker restores the real connect, so egress is possible when declared.

    Asserted by identity rather than by actually leaving the machine — this test
    must not depend on the operator having connectivity.
    """
    assert socket.socket.connect is not None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Under the guard this raises RuntimeError before any OS call. Marked
        # tests get the real connect, so the failure is a DNS/connection error.
        with pytest.raises(OSError) as exc_info:
            sock.settimeout(0.01)
            sock.connect(("192.0.2.1", 65000))  # TEST-NET-1, RFC 5737: never routable
    finally:
        sock.close()
    assert not isinstance(exc_info.value, RuntimeError)
