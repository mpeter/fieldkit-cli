"""auth/sf.py — Capture or directly inject a Salesforce sid cookie.

Two paths to the same cookie file:
  --sid VALUE   Direct write, no prompts, then session validation (e.g. value copied from browser DevTools).
  (no flags)    Interactive prompt walking through the DevTools steps, then
                validates the session against the live Salesforce API.

Either way, the sid is written to the configured cookie file in
``{"cookies": [<sid_cookie>]}`` format so the API client picks it up
immediately without requiring browser automation.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH
from fieldkit.config import get_cookie_file, get_salesforce_org_url, get_sf_rest_base_url
from fieldkit.config._loader import ConfigError
from fieldkit.protected_input import SecretInputError, read_secret_file

LOG_PREFIX = "[auth-sf]"


def _status_payload() -> dict[str, object]:
    """Return credential-safe Salesforce health fields for automation."""
    from fieldkit.commands.doctor.sf import check_sf

    result = check_sf()
    state = "authenticated" if result.healthy else "reauthorization-required" if result.configured else "unavailable"
    return {"service": result.service, "state": state, "configured": result.configured, "authenticated": result.healthy}


def _build_sid_cookie(sid: str, domain: str) -> dict[str, object]:
    """Build a canonical Salesforce sid cookie dict."""
    return {
        "name": "sid",
        "value": sid,
        "domain": f".{domain}",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "None",
    }


def _replace_cookie_file(cookie_file: Path, contents: bytes) -> None:
    """Replace the cookie file atomically with private-permission *contents*."""
    fd, temporary_name = tempfile.mkstemp(dir=cookie_file.parent, prefix=".fieldkit-sf-cookie-")
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
        temporary.replace(cookie_file)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_cookie_file(cookie_file: Path, previous_contents: bytes | None) -> None:
    """Restore the credential state that existed before a failed SID validation."""
    if previous_contents is None:
        cookie_file.unlink(missing_ok=True)
        return
    _replace_cookie_file(cookie_file, previous_contents)


def _write_sid_cookie(sid: str) -> None:
    """Write the sid cookie to the configured cookie file (0o600 permissions).

    Reads existing cookies first and updates any existing sid cookie for
    my.salesforce.com, or appends a new one.
    """
    cookie_file = get_cookie_file()
    cookie_file.parent.mkdir(parents=True, exist_ok=True)

    base_url = get_sf_rest_base_url()
    if not base_url:
        raise ConfigError(
            "Salesforce org URL is not configured. "
            "Set salesforce.org_url in accounts.yaml or sf_org_url in config.yaml."
        )
    domain = base_url.removeprefix("https://").removeprefix("http://")

    existing_cookies: list[dict[str, object]] = []
    if cookie_file.exists():
        try:
            data = json.loads(cookie_file.read_text(encoding="utf-8"))
            existing_cookies = data.get("cookies", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, OSError):
            pass  # fresh file

    canonical = _build_sid_cookie(sid, domain)
    updated = False
    new_cookies: list[dict[str, object]] = []
    for cookie in existing_cookies:
        if not isinstance(cookie, dict):
            new_cookies.append(cookie)
            continue
        if cookie.get("name") == "sid" and "my.salesforce.com" in str(cookie.get("domain", "")):
            new_cookies.append({**cookie, "value": sid})
            updated = True
        else:
            new_cookies.append(cookie)
    if not updated:
        new_cookies.append(canonical)

    encoded = json.dumps({"cookies": new_cookies}, indent=2).encode("utf-8")
    _replace_cookie_file(cookie_file, encoded)

    click.echo(f"{LOG_PREFIX} sid written to {cookie_file}", err=True)


def _prompt_for_sid() -> str:
    """Print step-by-step instructions and prompt the user to paste the sid value.

    Returns the stripped sid string.
    Raises SystemExit(2) if the user cancels (Ctrl+C / EOF).
    """
    # D3: interactive help-text path — must not raise ConfigError even when unconfigured.
    # get_salesforce_org_url() is safe (returns "" when absent); get_sf_rest_base_url()
    # raises ConfigError on malformed URLs so it is intentionally avoided here.
    sf_url = get_salesforce_org_url() or "your Salesforce org"

    click.echo("", err=True)
    click.echo("=" * 60, err=True)
    click.echo("  Salesforce Session Setup", err=True)
    click.echo("=" * 60, err=True)
    click.echo("", err=True)
    click.echo("You need a valid Salesforce sid cookie to authenticate API", err=True)
    click.echo("requests. Follow these steps:", err=True)
    click.echo("", err=True)
    click.echo(f"  1. Open  {sf_url}  in your browser", err=True)
    click.echo("  2. Log in with SSO if prompted", err=True)
    click.echo("  3. Open DevTools  (F12 on Windows/Linux, Cmd+Option+I on Mac)", err=True)
    click.echo("  4. Go to:  Application → Storage → Cookies", err=True)
    click.echo("     → select the domain that matches your org URL", err=True)
    click.echo("  5. Find the cookie named  'sid'  and copy its Value", err=True)
    click.echo("", err=True)
    click.echo("  Alternatively: DevTools → Network → reload the page → click", err=True)
    click.echo("  any request → Headers → find 'Cookie:' → copy only the 'sid=...'", err=True)
    click.echo("  portion (everything between 'sid=' and the next ';')", err=True)
    click.echo("", err=True)
    click.echo("Press Ctrl+C to cancel.", err=True)
    click.echo("", err=True)

    try:
        sid = str(click.prompt("Paste sid value", hide_input=True, err=True)).strip()
    except (click.Abort, EOFError, KeyboardInterrupt):
        # historic regression: Ctrl+C should be a clean exit (0), not an error (2).
        # Interactive cancellation is user-initiated, not a command failure.
        click.echo(f"\n{LOG_PREFIX} Cancelled.", err=True)
        raise SystemExit(0) from None

    return sid


def _validate_sid_format(sid: str) -> bool:
    """Basic format check — Salesforce sids contain '!' and are long."""
    return bool(sid) and "!" in sid and len(sid) > 20


def stdin_is_interactive() -> bool:
    """Return whether a human can safely answer a session-cookie prompt."""
    return sys.stdin.isatty()


def _emit_json_status(sid_file: Path | None) -> None:
    """Emit automation status, rejecting ambiguous credential injection."""
    if sid_file is not None:
        raise click.UsageError("--json cannot be combined with --sid-file")
    click.echo(json.dumps(_status_payload(), sort_keys=True))


def _warn_if_unusual_sid(sid: str, *, strict: bool) -> None:
    """Warn without rejecting a SID that does not resemble the usual format."""
    valid = _validate_sid_format(sid) if strict else bool(sid) and "!" in sid
    if valid:
        return
    click.echo(
        f"{LOG_PREFIX} WARNING: sid does not look like a valid Salesforce session ID "
        f"(expected format: 00DXXXXXXXXXXXXXXX!AQEA...). Proceeding anyway.",
        err=True,
    )


def _session_check() -> tuple[bool, str]:
    """Probe the saved Salesforce session after a credential write."""
    from fieldkit.commands.sf.session_check import check_sf_session

    return check_sf_session()


def _authenticate_injected_sid(sid: str) -> None:
    """Write, validate, and roll back a directly supplied Salesforce SID."""
    _warn_if_unusual_sid(sid, strict=False)
    cookie_file = get_cookie_file()
    previous_contents = cookie_file.read_bytes() if cookie_file.exists() else None
    _write_sid_cookie(sid)
    click.echo(f"{LOG_PREFIX} Validating session against Salesforce API...", err=True)
    alive, msg = _session_check()
    if alive:
        click.echo(f"{LOG_PREFIX} Session valid — {msg}", err=True)
        return
    _restore_cookie_file(cookie_file, previous_contents)
    click.echo(f"{LOG_PREFIX} ERROR: session check failed — {msg}", err=True)
    click.echo(f"{LOG_PREFIX} Previous credential state was restored.", err=True)
    click.echo(f"{LOG_PREFIX} Re-run: fieldkit auth sf", err=True)
    raise SystemExit(EXIT_AUTH)


def _require_interactive_sid() -> str:
    """Refuse a prompt when stdin cannot safely accept a human response."""
    if stdin_is_interactive():
        return _prompt_for_sid()
    click.echo(f"{LOG_PREFIX} Authentication requires an interactive terminal.", err=True)
    click.echo(f"{LOG_PREFIX} Reauthorize interactively with: fieldkit auth sf", err=True)
    click.echo(
        f"{LOG_PREFIX} Automation may use an owner-only sid file with: fieldkit auth sf --sid-file PATH", err=True
    )
    raise SystemExit(EXIT_AUTH)


def _authenticate_prompted_sid() -> None:
    """Prompt for, write, and validate a human-supplied Salesforce SID."""
    prompted_sid = _require_interactive_sid()
    _warn_if_unusual_sid(prompted_sid, strict=True)
    _write_sid_cookie(prompted_sid)
    click.echo(f"{LOG_PREFIX} Validating session against Salesforce API...", err=True)
    alive, msg = _session_check()
    if not alive:
        click.echo(f"{LOG_PREFIX} ERROR: session check failed — {msg}", err=True)
        click.echo(f"{LOG_PREFIX} The sid you entered may be expired or invalid.", err=True)
        click.echo(f"{LOG_PREFIX} Re-run: fieldkit auth sf", err=True)
        raise SystemExit(EXIT_AUTH)
    click.echo(f"{LOG_PREFIX} Session valid — {msg}", err=True)
    click.echo(f"{LOG_PREFIX} Done. Cookie file: {get_cookie_file()}", err=True)


@click.command(name="sf")
@click.option(
    "--sid-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Read a sid from an owner-only regular file, skipping the interactive prompt but validating the session.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Emit credential-safe authentication health as JSON."
)
def auth_sf_cmd(sid_file: Path | None, as_json: bool) -> None:
    """Authenticate the Salesforce session via a sid cookie.

    Without ``--sid-file``: walks through the DevTools steps interactively,
    then validates the session against the live Salesforce API.

    With ``--sid-file PATH``: reads an owner-only file containing the copied
    browser value and validates the session without prompting.
    """
    if as_json:
        _emit_json_status(sid_file)
        return

    if sid_file is not None:
        try:
            _authenticate_injected_sid(read_secret_file(sid_file, label="Salesforce sid"))
        except SecretInputError as exc:
            raise click.UsageError(str(exc)) from exc
        return

    _authenticate_prompted_sid()
