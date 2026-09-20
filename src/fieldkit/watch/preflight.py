"""Pre-flight connectivity checks for heavy fieldkit operations (implementation note).

Provides a fast, local probe for each required service before starting
long-running operations. All checks complete in < 3 seconds total.

Usage:
    from fieldkit.watch.preflight import preflight_check

    failures = preflight_check(["sf", "gmail", "mcp", "llm"])
    if failures:
        for msg in failures:
            click.echo(f"Pre-flight check failed: {msg}", err=True)
        sys.exit(1)
"""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fieldkit.config import llm_disabled

# Type alias: a check function returns an error message string on failure, or
# None if the service is available. Functions MUST NOT raise exceptions.
CheckFn = Callable[[], str | None]


def _check_sf_session() -> str | None:
    """Check that the Salesforce session cookie file exists and contains a 'sid' entry.

    Reads the cookie file path from config; does NOT make any HTTP requests.

    Returns:
        None if the SF session is present, or an error message string if not.
    """
    from fieldkit.config import get_cookie_file

    cookie_file = get_cookie_file()
    if not cookie_file.exists():
        return f"SF session not found: {cookie_file} — run 'fieldkit auth sf'"
    try:
        data = json.loads(cookie_file.read_text(encoding="utf-8"))
        cookies = data.get("cookies", []) if isinstance(data, dict) else []
        if not any(c.get("name") == "sid" for c in cookies if isinstance(c, dict)):
            return f"SF session cookie 'sid' not found in {cookie_file} — run 'fieldkit auth sf'"
    except Exception:  # noqa: BLE001
        # Any parse/IO failure is surfaced as a user-facing message, never raised.
        return f"SF session file unreadable: {cookie_file} — run 'fieldkit auth sf'"
    return None


def _check_gmail_token() -> str | None:
    """Check that the Gmail OAuth token file exists and has not expired.

    Reads the token file path from config; does NOT make any HTTP requests.

    Returns:
        None if the token is present and valid, or an error message string if not.
    """
    from fieldkit.config import get_google_token_path

    token_path = get_google_token_path()
    if not token_path.exists():
        return f"Gmail OAuth token not found: {token_path} — run 'fieldkit doctor google'"
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
        # google-auth libraries use both "expiry" and "token_expiry" key names.
        expiry_str = data.get("expiry") or data.get("token_expiry")
        if expiry_str:
            expiry = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
            if expiry < datetime.now(tz=UTC):
                return f"Gmail OAuth token expired at {expiry_str} — run 'fieldkit doctor google'"
    except Exception:  # noqa: BLE001
        # Any parse/IO failure is surfaced as a user-facing message, never raised.
        return f"Gmail OAuth token unreadable: {token_path} — run 'fieldkit doctor google'"
    return None


def _check_mcp_reachable() -> str | None:
    """Check that mcpjungle is reachable at the configured MCP gateway base URL.

    Makes a single HTTP GET with a 2-second timeout. Returns a failure message
    on ConnectError, non-200 response, or any other exception. NEVER raises.

    Returns:
        None if mcpjungle responds with HTTP 200, or an error message string.
    """
    import httpx

    from fieldkit.config import get_mcp_gateway_base

    base = get_mcp_gateway_base()
    try:
        response = httpx.get(f"{base}/", timeout=2.0)
        if response.status_code != 200:
            return f"mcpjungle returned HTTP {response.status_code} at {base}/ — run: systemctl --user start mcpjungle"
        return None
    except httpx.ConnectError:
        return f"mcpjungle not reachable at {base}/ — run: systemctl --user start mcpjungle"
    except Exception as exc:  # noqa: BLE001
        # Catch all other exceptions (timeout, SSL, etc.) — never raise from a check.
        return f"mcpjungle check failed: {exc}"


def _check_llm_available() -> str | None:
    """Check that Google Application Default Credentials are available for LLM calls.

    Skipped entirely when LLM calls are disabled (via llm_disabled() from
    fieldkit.config, which checks both FIELDKIT_NO_LLM and NO_LLM).
    Checks GOOGLE_APPLICATION_CREDENTIALS env var or the well-known ADC file.
    Does NOT make any HTTP requests.

    Returns:
        None if credentials are available (or LLM is disabled), or an error message.
    """
    # historic regression: use llm_disabled() — single source of truth for both FIELDKIT_NO_LLM
    # and NO_LLM, instead of inline os.environ.get() checks.
    if llm_disabled():
        return None  # LLM disabled — check not applicable

    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or adc.exists():
        return None

    return "Google Application Default Credentials not found — run: gcloud auth application-default login"


#: Maps service name → check function. Order is not significant here;
#: preflight_check() iterates the caller-supplied services list.
_CHECKS: dict[str, CheckFn] = {
    "sf": _check_sf_session,
    "gmail": _check_gmail_token,
    "mcp": _check_mcp_reachable,
    "llm": _check_llm_available,
}


def preflight_check(services: list[str], *, dry_run: bool = False) -> list[str]:
    """Run connectivity probes for the listed services.

    Each probe is fast (< 1 second) and local — no blocking network calls
    except for the MCP check (2-second timeout). All checks complete in
    < 3 seconds total.

    When ``dry_run=True``, the ``mcp`` and ``llm`` checks are skipped because
    those services are not required for a dry run.

    Args:
        services: Names of services to check. Valid values: "sf", "gmail",
            "mcp", "llm". Unknown names are silently ignored.
        dry_run: When True, skip the "mcp" and "llm" checks.

    Returns:
        A list of failure message strings. An empty list means all checks passed.
    """
    failures: list[str] = []
    for service in services:
        # Dry-run skips MCP and LLM — they may not be needed for a dry run.
        if dry_run and service in ("mcp", "llm"):
            continue
        check_fn = _CHECKS.get(service)
        if check_fn is None:
            continue
        msg = check_fn()
        if msg:
            failures.append(msg)
    return failures
