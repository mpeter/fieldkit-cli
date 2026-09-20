#!/usr/bin/env python3
"""oc_event_watcher.py — tail the opencode serve SSE event stream and forward
notable events to an aoe session via `aoe send`.

Usage:
    ./scripts/oc_event_watcher.py [AOE_SESSION_TITLE]

    AOE_SESSION_TITLE  aoe session to alert (default: fieldkit-oc-bridge)

Environment:
    OPENCODE_SERVER_PASSWORD   required — opencode serve bearer password
    OPENCODE_SERVER_USERNAME   optional — defaults to "opencode"
    OPENCODE_SERVER_URL        optional — defaults to http://localhost:4096

Requires: httpx (already in pyproject deps)
"""

import json
import logging
import os
import subprocess
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AOE_TARGET = sys.argv[1] if len(sys.argv) > 1 else "fieldkit-oc-bridge"
OC_URL = os.environ.get("OPENCODE_SERVER_URL", "http://localhost:4096")
OC_USER = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OC_PASS = os.environ.get("OPENCODE_SERVER_PASSWORD", "")

ALERT_TYPES = {
    "error",
    "failed",
    "abort",
    "permission",
    "message.completed",
    "session.updated",
}

RETRY_DELAY_SECS = 5
REQUEST_TIMEOUT_SECS = 90  # SSE keepalive heartbeat window

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[oc-watcher %(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("oc_event_watcher")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def send_alert(message: str) -> None:
    """Forward *message* to the aoe session via `aoe send`."""
    log.info("→ aoe send [%s]: %s", AOE_TARGET, message)
    try:
        subprocess.run(
            ["aoe", "send", AOE_TARGET, message],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("aoe send failed (session may be stopped): %s", exc.stderr.strip())
    except FileNotFoundError:
        log.error("'aoe' binary not found in PATH")


def _summarise(payload: dict) -> str:
    """Build a compact human-readable summary of an SSE event payload."""
    event_type = payload.get("type", "?")
    props = payload.get("properties") or {}

    sid = props.get("sessionID") or props.get("session") or ""
    sid_label = f"[…{sid[-8:]}] " if sid else ""

    detail = props.get("message") or props.get("error") or props.get("reason") or props.get("title") or str(props)[:80]

    return f"opencode event: {sid_label}{event_type} — {str(detail)[:120]}"


def _is_alert(event_type: str) -> bool:
    """Return True if this event type should be forwarded."""
    lower = event_type.lower()
    return any(lower == t or lower.startswith(t) for t in ALERT_TYPES)


# ---------------------------------------------------------------------------
# SSE stream consumer
# ---------------------------------------------------------------------------


def watch_once(client: httpx.Client) -> None:
    """Open the SSE stream and read until the connection closes."""
    log.info("Connecting to %s/event …", OC_URL)
    with client.stream(
        "GET",
        f"{OC_URL}/event",
        headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        timeout=REQUEST_TIMEOUT_SECS,
    ) as response:
        response.raise_for_status()
        log.info("Connected (HTTP %s)", response.status_code)

        for raw_line in response.iter_lines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue

            payload_str = line[len("data:") :].strip()
            if not payload_str:
                continue

            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                log.debug("Non-JSON SSE data: %s", payload_str[:80])
                continue

            event_type = payload.get("type", "")
            if not event_type:
                continue

            if _is_alert(event_type):
                send_alert(_summarise(payload))


# ---------------------------------------------------------------------------
# Main retry loop
# ---------------------------------------------------------------------------


def main() -> None:
    if not OC_PASS:
        log.error("OPENCODE_SERVER_PASSWORD is not set. Export it before running this script.")
        sys.exit(1)

    log.info(
        "Starting opencode event watcher → aoe session %r  |  source: %s/event",
        AOE_TARGET,
        OC_URL,
    )

    auth = (OC_USER, OC_PASS)

    while True:
        try:
            with httpx.Client(auth=auth) as client:
                watch_once(client)
            log.info("SSE stream closed cleanly, reconnecting in %ds …", RETRY_DELAY_SECS)
        except httpx.HTTPStatusError as exc:
            log.warning("HTTP %s — reconnecting in %ds …", exc.response.status_code, RETRY_DELAY_SECS)
        except httpx.RequestError as exc:
            log.warning("Connection error (%s) — reconnecting in %ds …", exc, RETRY_DELAY_SECS)
        except KeyboardInterrupt:
            log.info("Interrupted — exiting.")
            sys.exit(0)

        time.sleep(RETRY_DELAY_SECS)


if __name__ == "__main__":
    main()
