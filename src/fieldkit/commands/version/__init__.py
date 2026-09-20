"""fieldkit version — version info and feature introspection.

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

import json
import logging
import platform
import sys
import urllib.error
import urllib.request
from typing import Any

import click

# ---------------------------------------------------------------------------
# Re-export implementation surface from _impl.
#
# Design note: _probe_chrome_debug and _collect_features are defined in this
# module (not delegated to _impl) so that ``patch("fieldkit.commands.version.Path", …)``
# in the test suite correctly intercepts the Path constructor.  All other
# functions live in _impl.py and are re-exported here for a stable public API.
# ---------------------------------------------------------------------------
from fieldkit.commands.version._impl import _build_group_entry as _build_group_entry
from fieldkit.commands.version._impl import _fieldkit_version as _fieldkit_version
from fieldkit.commands.version._impl import _icon as _icon
from fieldkit.commands.version._impl import _print_config as _print_config
from fieldkit.commands.version._impl import _print_groups as _print_groups
from fieldkit.commands.version._impl import _print_issues as _print_issues
from fieldkit.commands.version._impl import _print_services as _print_services
from fieldkit.commands.version._impl import _print_skills as _print_skills
from fieldkit.commands.version._impl import _probe_config as _probe_config
from fieldkit.commands.version._impl import _probe_gmail as _probe_gmail
from fieldkit.commands.version._impl import _probe_groups as _probe_groups
from fieldkit.commands.version._impl import _probe_issues as _probe_issues
from fieldkit.commands.version._impl import _probe_mcpjungle as _probe_mcpjungle
from fieldkit.commands.version._impl import _probe_sf_cookies as _probe_sf_cookies
from fieldkit.commands.version._impl import _probe_sf_token as _probe_sf_token
from fieldkit.commands.version._impl import _probe_skills as _probe_skills
from fieldkit.commands.version._impl import _render_features_human as _render_features_human
from fieldkit.commands.version._impl import _render_version_human as _render_version_human
from fieldkit.commands.version._impl import _sub_entry as _sub_entry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _probe_chrome_debug — kept in __init__ so _collect_features uses this module's
# public probe implementation.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _collect_features — kept in __init__ so it calls the local _probe_chrome_debug
# rather than _impl's copy.
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


# ---------------------------------------------------------------------------
# main — kept in __init__ so that ``from fieldkit.commands.version import main`` works
# and so it calls the local _collect_features (which uses local _probe_chrome_debug).
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    want_features = "--features" in argv or "-f" in argv
    want_json = "--json" in argv

    if "--help" in argv or "-h" in argv:
        click.echo(__doc__)
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
