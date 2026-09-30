"""SF session health check — verify the stored cookie is still valid.

historic regression: Previously this probe used Cookie auth while SFDirectClient uses
Bearer auth (Authorization: Bearer {sid}). A 200 from the cookie path did
not guarantee subsequent API calls would succeed. This module now uses the
same Bearer auth as SFDirectClient, so a passing check_sf_session() means
the sid is genuinely valid for API calls.

historic regression: the probe endpoint itself was also wrong. GET /services/data/ only
lists supported API versions -- confirmed live 2026-08-15 that a sid which
passes this check can still get a real HTTP 401 INVALID_SESSION_ID on every
actual object-level call (sObject GET, describe, UI API). This module now
probes GET /services/data/{version}/sobjects/Account/describe instead --
Account is the object fieldkit already uses most. Organization is not a safe
probe substitute because some permission sets return 404 for that object even
when the session is healthy; treating that response as expiry would be wrong.
describe is metadata-only (no data rows) and genuinely requires the same
auth depth as real SFDirectClient calls, so a passing check_sf_session() now
means what it says.
"""

import logging
import time

import click
import httpx

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.config import TIMEOUT_SF_SESSION_CHECK, get_cookie_file
from fieldkit.config.salesforce_cookie import read_salesforce_cookie
from fieldkit.sf.client import API_VERSION
from fieldkit.sf.errors import reauth_hint_message as _reauth_hint

log = logging.getLogger(__name__)


def check_sf_session() -> tuple[bool, str]:
    """Return (is_alive, message).

    Loads sf-cookies.json, extracts the sid and SF instance URL, then probes
    a real object-level endpoint (sobjects/Account/describe) using
    Bearer auth — the same mechanism and the same auth depth used by real
    SFDirectClient calls — so this check and live API calls agree on session
    validity (historic regression: GET /services/data/ alone does not).

    Returns (True, message) if 200, (False, message) if expired/missing.
    """
    cookie_file = get_cookie_file()
    try:
        sid_cookie = read_salesforce_cookie(cookie_file)
    except FileNotFoundError:
        return False, f"Cookie file not found; {_reauth_hint()}"
    except (ValueError, OSError):
        return False, f"Failed to read cookie file; {_reauth_hint()}"
    if not sid_cookie:
        return False, "No 'sid' cookie found in cookie file (need domain: *.my.salesforce.com)"

    sf_base = f"https://{sid_cookie.domain}"

    for attempt in range(2):
        try:
            resp = httpx.get(
                f"{sf_base}/services/data/{API_VERSION}/sobjects/Account/describe",
                headers={"Authorization": f"Bearer {sid_cookie.sid}"},
                follow_redirects=False,
                timeout=TIMEOUT_SF_SESSION_CHECK,
            )
            if resp.status_code == 200:
                return True, "Salesforce API session is active."
            elif resp.status_code in (401, 403):
                return (
                    False,
                    f"SF session expired — {_reauth_hint()}",
                )
            elif resp.status_code in (301, 302, 303):
                return (
                    False,
                    f"SF session expired (redirect to login) — {_reauth_hint()}",
                )
            else:
                return (
                    False,
                    f"Unexpected response HTTP {resp.status_code}; check Salesforce availability and permissions.",
                )
        except httpx.RequestError:
            if attempt == 0:
                # implementation note: simple two-attempt retry; tenacity not needed here.
                log.debug("Network error on attempt 1; retrying Salesforce session check.")
                time.sleep(1)
                continue
            return False, "Network error checking SF session; check connectivity and retry."
    # unreachable, but satisfies mypy
    return False, "Network error checking SF session: exhausted retries"


@click.command(
    name="session-check",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def cli(as_json: bool) -> None:
    """Check whether the Salesforce session cookie is still valid.

    Exits 0 when the session is active, 2 when expired or missing.
    """
    alive, msg = check_sf_session()
    verdict = "ACTIVE" if alive else "EXPIRED"

    if as_json:
        import json as _json

        click.echo(_json.dumps({"verdict": verdict, "message": msg, "active": alive}))
    else:
        click.echo(f"Session: {verdict}")
        click.echo(msg)

    raise SystemExit(EXIT_SUCCESS if alive else EXIT_AUTH)
