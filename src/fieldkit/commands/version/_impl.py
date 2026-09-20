"""fieldkit version — implementation.

All probe functions, rendering helpers, and the main() entry point live here.
``fieldkit/version/__init__.py`` re-exports the public surface.
"""

import importlib.metadata
import importlib.resources
import json
import logging
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import click

from fieldkit.config import get_cookie_file
from fieldkit.config.optional_dependencies import SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY

logger = logging.getLogger(__name__)

# User-facing help text (mirrors fieldkit/version/__init__.py module docstring).
_HELP = """\
fieldkit version — version info and feature introspection.

Usage:
    fieldkit version
    fieldkit version --features
    fieldkit version --features --json

Plain `fieldkit version` prints version, Python, and platform — quick human read.

`--features` adds a structured capability report:
  - CLI groups and their subcommand counts
  - Skills available in the skills/ directory
  - Config reachability (data root, accounts, identity)
  - Service status (mcpjungle, gmail.db, SF token, Chrome debug port)

`--json` emits everything as machine-readable JSON — intended for agents that
need to choose a workflow based on what is actually installed and reachable.
"""

# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


def _fieldkit_version() -> str:
    try:
        return importlib.metadata.version("fieldkit-cli")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


# ---------------------------------------------------------------------------
# Feature probes
# ---------------------------------------------------------------------------


def _sub_entry(sub_name: str, sub_val: Any) -> dict[str, str]:
    """Convert a single SUBCOMMANDS entry into a {name, description} dict."""
    if isinstance(sub_val, tuple) and len(sub_val) >= 2:
        return {"name": sub_name, "description": sub_val[1]}
    if isinstance(sub_val, str):
        return {"name": sub_name, "description": sub_val}
    return {"name": sub_name, "description": ""}


def _build_group_entry(name: str, desc: str, module_path: str) -> dict[str, Any]:
    """Import one CLI group module and build its probe entry."""
    entry: dict[str, Any] = {"group": name, "description": desc}
    try:
        mod = __import__(module_path, fromlist=["SUBCOMMANDS", "DESCRIPTION", "USAGE", "FLAGS", "cli"])
        subs = getattr(mod, "SUBCOMMANDS", None)
        if isinstance(subs, dict):
            # Legacy dict-style registration (still used by brief and similar)
            entry["subcommands"] = [_sub_entry(k, v) for k, v in subs.items()]
        else:
            # Try to introspect a Click group registered as `cli`
            group_obj = getattr(mod, "cli", None)
            if isinstance(group_obj, click.Group):
                ctx = click.Context(group_obj)
                ctx.meta[SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY] = True
                entry["subcommands"] = [
                    {
                        "name": n,
                        "description": (
                            cmd.help if isinstance(cmd := group_obj.get_command(ctx, n), click.Command) else ""
                        ),
                    }
                    for n in group_obj.list_commands(ctx)
                ]
            else:
                # Direct-dispatch module: expose usage + flags, no subcommands
                entry["subcommands"] = []
                entry["usage"] = getattr(mod, "USAGE", f"fieldkit {name}")
                flags = getattr(mod, "FLAGS", None)
                if flags:
                    entry["flags"] = [{"flag": k, "description": v} for k, v in flags.items()]
    except Exception:  # noqa: BLE001
        logger.debug("Subcommand introspection failed for %s", name, exc_info=True)
        entry["subcommands"] = []
    return entry


def _probe_groups() -> list[dict[str, Any]]:
    """Return each CLI group with full subcommand detail."""
    from fieldkit.__main__ import _COMMANDS as GROUPS

    return [_build_group_entry(name, desc, mod_path) for name, (desc, mod_path) in sorted(GROUPS.items())]


def _probe_skills() -> dict[str, Any]:
    """List skills available in the skills/ directory (via fieldkit.skill._runner)."""
    try:
        from fieldkit.commands.skill._runner import _load_all_skills

        skills = _load_all_skills()
        return {
            "available": True,
            "count": len(skills),
            "skills": [
                {
                    "name": s["name"],
                    "description": s["description"].split(".")[0].strip() if s["description"] else "",
                    "user_invocable": s["user_invocable"],
                    "argument_hint": s["argument_hint"],
                }
                for s in skills
            ],
        }
    except Exception as exc:  # noqa: BLE001
        try:
            skills_dir = importlib.resources.files("fieldkit.skills")
        except Exception:  # noqa: BLE001
            skills_dir = Path(__file__).resolve()
        return {"available": False, "count": 0, "error": str(exc), "path": str(skills_dir)}


def _probe_config() -> dict[str, Any]:
    """Check whether fieldkit workspace config is reachable."""
    try:
        from fieldkit.config import get_fieldkit_home

        data_root = get_fieldkit_home()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}

    result: dict[str, Any] = {
        "available": data_root is not None,
        "data_root": str(data_root) if data_root else None,
    }
    if not data_root:
        return result

    # Accounts
    accounts_yaml = data_root / "config" / "accounts.yaml"
    if accounts_yaml.is_file():
        try:
            import yaml

            with accounts_yaml.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            result["accounts"] = sorted(cfg.get("accounts", {}).keys()) if cfg else []
        except Exception:  # noqa: BLE001
            result["accounts"] = []
    else:
        result["accounts"] = []

    # Identity
    identity_yaml = data_root / "config" / "identity.yaml"
    result["identity_configured"] = identity_yaml.is_file()

    return result


def _probe_gmail() -> dict[str, Any]:
    """Check for a local gmail.db cache."""
    from fieldkit.gmail.discover import get_gmail_db_path

    db = get_gmail_db_path()
    # implementation change/historic regression: show ~/abbreviated path, not absolute
    display_path = str(db).replace(str(Path.home()), "~")
    if db.exists():
        size_bytes = db.stat().st_size
        if size_bytes == 0:
            return {"available": False, "path": display_path, "error": "0-byte database"}
        size_mb = round(size_bytes / (1024 * 1024), 1)
        return {"available": True, "path": display_path, "size_mb": size_mb}
    return {"available": False, "path": display_path}


def _probe_sf_token() -> dict[str, Any]:
    """Check whether SF cookie-based auth is configured.

    implementation change: fieldkit uses cookie-based SF auth (not SF_ACCESS_TOKEN env var).
    Check for the presence of the sf-cookies.json file with a valid sid cookie.
    """
    from fieldkit.config import get_cookie_file

    cookie_file = get_cookie_file()
    if not cookie_file.exists():
        return {"available": False, "method": "cookie"}
    try:
        import json as _json

        data = _json.loads(cookie_file.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        has_sid = any(c.get("name") == "sid" and c.get("value") for c in cookies)
        return {"available": has_sid, "method": "cookie"}
    except Exception:  # noqa: BLE001
        return {"available": False, "method": "cookie"}


def _probe_chrome_debug() -> dict[str, Any]:
    """Check whether Chrome is available via CDP port 9222."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2):
            return {"available": True, "port": 9222, "via": "cdp"}
    except (urllib.error.URLError, OSError):
        logger.debug(
            "version: CDP port probe failed — Chrome not running with --remote-debugging-port=9222", exc_info=True
        )

    return {"available": False, "port": 9222, "via": None}


def _probe_mcpjungle() -> dict[str, Any]:
    """Check whether mcpjungle is responding at the configured MCP gateway URL."""
    from fieldkit.config import get_mcp_gateway_url

    base = get_mcp_gateway_url()
    try:
        with urllib.request.urlopen(f"{base}/", timeout=3):
            pass
        return {"available": True, "url": base}
    except (urllib.error.URLError, OSError):
        return {"available": False, "url": base}


def _probe_sf_cookies() -> dict[str, Any]:
    """Check for a saved Salesforce browser session cookie file."""
    cookie_file = get_cookie_file()
    if not cookie_file.exists():
        return {"available": False}
    try:
        with cookie_file.open(encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        sid_cookies = [c for c in cookies if c.get("name") == "sid"]
        return {
            "available": True,
            "cookie_count": len(cookies),
            "session_active": len(sid_cookies) > 0,
        }
    except Exception:  # noqa: BLE001
        return {"available": True, "session_active": False}


# ---------------------------------------------------------------------------
# Feature report assembly
# ---------------------------------------------------------------------------


def _collect_features() -> dict[str, Any]:
    return {
        "cli": {
            "version": _fieldkit_version(),
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "arch": platform.machine(),
            "groups": _probe_groups(),
        },
        "skills": _probe_skills(),
        "config": _probe_config(),
        "services": {
            "mcpjungle": _probe_mcpjungle(),
            "gmail_cache": _probe_gmail(),
            "sf_token": _probe_sf_token(),
            "sf_cookies": _probe_sf_cookies(),
            "chrome_debug": _probe_chrome_debug(),
        },
        "issues": _probe_issues(),
    }


def _probe_issues() -> dict[str, Any]:
    """Count open issues via the GitHub Issues API.

    Uses get_github_repo() from config to query gh CLI for open bug/enhancement
    counts. Returns {"available": False, ...} when github_repo is not configured
    or gh is unavailable.
    """
    import subprocess

    from fieldkit.config import ConfigError, get_github_repo

    try:
        repo = get_github_repo()
    except ConfigError as exc:
        return {
            "available": False,
            "error": str(exc),
            "open_bugs": 0,
            "open_enhancements": 0,
            "closed": 0,
            "wontfix": 0,
        }

    def _count(label: str, state: str) -> int:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    state,
                    "--label",
                    label,
                    "--limit",
                    "1000",
                    "--json",
                    "number",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                return 0
            import json

            return len(json.loads(result.stdout or "[]"))
        except Exception:  # noqa: BLE001
            return 0

    return {
        "available": True,
        "repo": repo,
        "open_bugs": _count("bug", "open"),
        "open_enhancements": _count("enhancement", "open"),
        "closed": _count("bug", "closed") + _count("enhancement", "closed"),
        "wontfix": 0,  # wont-fix issues are closed with state_reason=not_planned; not separately labelable here
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _icon(ok: bool) -> str:
    return "✓" if ok else "✗"


def _print_groups(groups: list[dict[str, Any]]) -> None:
    click.echo("Groups:")
    for g in groups:
        subs = g.get("subcommands", [])
        click.echo(f"  {g['group']}")
        if subs:
            click.echo(f"    {'  '.join(s['name'] for s in subs)}")
        else:
            default_usage = f"fieldkit {g['group']}"
            click.echo(f"    {g.get('usage', default_usage)}")
    click.echo()


def _print_skills(skills: dict[str, Any]) -> None:
    if not skills["available"]:
        click.echo(f"Skills: directory not found ({skills.get('error', '')})")
        click.echo()
        return

    click.echo(f"Skills: {skills['count']} available")
    line = "  "
    for s in skills.get("skills", []):
        name = s["name"]
        if len(line) + len(name) + 2 > 80:
            click.echo(line)
            line = "  " + name
        else:
            line += ("  " if line != "  " else "") + name
    if line.strip():
        click.echo(line)
    click.echo()


def _print_config(cfg: dict[str, Any]) -> None:
    click.echo("Config:")
    if not cfg["available"]:
        click.echo(f"  {_icon(False)} data root    {cfg.get('error', 'not found')}")
        click.echo()
        return

    click.echo(f"  {_icon(True)} data root    {cfg['data_root']}")
    accts = cfg.get("accounts", [])
    click.echo(f"  {_icon(bool(accts))} accounts     {', '.join(accts) if accts else 'none configured'}")
    identity_ok = cfg.get("identity_configured", False)
    click.echo(f"  {_icon(identity_ok)} identity     {'configured' if identity_ok else 'not configured'}")
    click.echo()


def _print_services(svcs: dict[str, Any]) -> None:
    click.echo("Services:")

    mcp = svcs["mcpjungle"]
    mcp_label = f"running at {mcp['url']}" if mcp["available"] else "not running"
    click.echo(f"  {_icon(mcp['available'])} mcpjungle    {mcp_label}")

    gm = svcs["gmail_cache"]
    if gm["available"]:
        click.echo(f"  {_icon(True)} gmail cache  {gm['size_mb']} MB  ({gm['path']})")
    else:
        click.echo(f"  {_icon(False)} gmail cache  not found")

    # implementation change: consolidate SF token + cookies rows into a single SF session line.
    # Both probes check the same cookie file — showing both caused a confusing ✗ SF token
    # alongside ✓ SF cookies, making the auth state appear broken when it was fine.
    sf_ck = svcs["sf_cookies"]
    if sf_ck.get("available"):
        session_str = "active" if sf_ck.get("session_active") else "no active session"
        click.echo(f"  {_icon(sf_ck.get('session_active', False))} SF session   {session_str}")
    else:
        click.echo(f"  {_icon(False)} SF session   not configured (run: fieldkit auth sf)")

    ch = svcs["chrome_debug"]
    chrome_label = f"debugging on port {ch['port']}" if ch["available"] else "not running"
    click.echo(f"  {_icon(ch['available'])} chrome       {chrome_label}")


def _print_issues(issues: dict[str, Any]) -> None:
    if not issues:
        return
    click.echo("Issues:")
    click.echo(f"  repo: {issues.get('repo', '(unknown)')}")
    click.echo(f"  open: {issues.get('open_bugs', 0)} bug(s), {issues.get('open_enhancements', 0)} enhancement(s)")
    click.echo(f"  closed: {issues.get('closed', 0)}")


def _render_features_human(features: dict[str, Any]) -> None:
    cli = features["cli"]
    click.echo(f"fieldkit {cli['version']}  python {cli['python']}  {cli['platform']} {cli['arch']}")
    click.echo()
    _print_groups(cli["groups"])
    _print_skills(features["skills"])
    _print_config(features["config"])
    _print_services(features["services"])
    _print_issues(features.get("issues", {}))


def _render_version_human() -> None:
    ver = _fieldkit_version()
    py = sys.version.split()[0]
    plat = platform.system()
    arch = platform.machine()
    click.echo(f"fieldkit {ver}  python {py}  {plat} {arch}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    want_features = "--features" in argv or "-f" in argv
    want_json = "--json" in argv

    if "--help" in argv or "-h" in argv:
        click.echo(_HELP)
        return 0

    if want_features:
        features = _collect_features()
        if want_json:
            click.echo(json.dumps(features, indent=2))
        else:
            _render_features_human(features)
        return 0

    # Plain version — when --json is requested, always emit the full feature
    # structure so that `version --json` includes services/config/skills/groups
    # (implementation change).  The base 4 fields (version, python, platform, arch) are kept
    # at the top level for backward compatibility; the richer feature sections
    # are merged in alongside them.
    if want_json:
        features = _collect_features()
        cli_section = features.pop("cli", {})
        payload: dict[str, Any] = {
            "version": cli_section.get("version", _fieldkit_version()),
            "python": cli_section.get("python", sys.version.split()[0]),
            "platform": cli_section.get("platform", platform.system()),
            "arch": cli_section.get("arch", platform.machine()),
            "groups": cli_section.get("groups", []),
            **features,
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        _render_version_human()
    return 0
