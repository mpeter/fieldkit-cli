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

import json
import logging
import time

import click
import httpx

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.config import TIMEOUT_SF_SESSION_CHECK, get_cookie_file
from fieldkit.sf.client import reauth_hint_message as _reauth_hint

log = logging.getLogger(__name__)

# historic regression: must match fieldkit.sf.client._API_VERSION. Kept as a separate
# local constant (not a cross-module import of a private name) since this is
# the only other place an API version is hardcoded for a raw probe request.
_PROBE_API_VERSION = "v59.0"


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
    if not cookie_file.exists():
        return False, f"Cookie file not found: {cookie_file}"

    try:
        data = json.loads(cookie_file.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to read cookie file: {exc}"

    # Find the sid cookie — used as Bearer token (matching SFDirectClient)
    sid_cookie = next(
        (c for c in cookies if c.get("name") == "sid" and "my.salesforce.com" in c.get("domain", "")),
        None,
    )
    if not sid_cookie:
        return False, "No 'sid' cookie found in cookie file (need domain: *.my.salesforce.com)"

    sid = sid_cookie.get("value", "")
    if not sid:
        return (
            False,
            f"SF sid cookie has no value — {_reauth_hint()}",
        )

    cookie_domain = sid_cookie.get("domain", "").lstrip(".")
    sf_base = f"https://{cookie_domain}"

    for attempt in range(2):
        try:
            resp = httpx.get(
                f"{sf_base}/services/data/{_PROBE_API_VERSION}/sobjects/Account/describe",
                headers={"Authorization": f"Bearer {sid}"},
                follow_redirects=False,
                timeout=TIMEOUT_SF_SESSION_CHECK,
            )
            if resp.status_code == 200:
                return True, f"instance: {cookie_domain}"
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
                return False, f"Unexpected response {resp.status_code} from {sf_base}"
        except httpx.RequestError as exc:
            if attempt == 0:
                # implementation note: simple two-attempt retry; tenacity not needed here.
                log.debug("Network error on attempt 1, retrying: %s", exc)
                time.sleep(1)
                continue
            return False, f"Network error checking SF session: {exc}"
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
