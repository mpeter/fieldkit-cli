"""Tests for fieldkit version — version info and feature introspection.

Covers:
- Plain version output (no flags)
- --version / -V shortcuts in __main__
- --json flag for machine-readable output
- --features flag (rendered and JSON)
- Individual probe functions (config, skills, groups, services)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.version import (
    _probe_chrome_debug,
    _probe_config,
    _probe_gmail,
    _probe_groups,
    _probe_mcpjungle,
    _probe_sf_token,
    _probe_skills,
    main,
)
from fieldkit.commands.version._impl import _probe_issues

pytestmark = pytest.mark.unit

# Stub returned by _probe_issues mock — avoids 4 live GitHub API calls (~5s)
# in tests that don't exercise issue-probe logic specifically.
_STUB_ISSUES = {
    "available": True,
    "repo": "test/repo",
    "open_bugs": 5,
    "open_enhancements": 3,
    "closed": 10,
    "wontfix": 0,
}


# ---------------------------------------------------------------------------
# Plain version
# ---------------------------------------------------------------------------


# ── TestPlainVersion (flattened) ────────────────────────────────────────────


def test_fieldkit_version_returns_0(capsys: pytest.CaptureFixture) -> None:
    rc = main([])
    assert rc == 0


def test_fieldkit_version_prints_version_string(capsys: pytest.CaptureFixture) -> None:
    main([])
    out = capsys.readouterr().out
    assert "fieldkit" in out
    assert "python" in out


def test_fieldkit_version_json_flag_emits_json(capsys: pytest.CaptureFixture) -> None:
    with patch("fieldkit.commands.version._probe_issues", return_value=_STUB_ISSUES):
        main(["--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "version" in data
    assert "python" in data
    assert "platform" in data
    assert "arch" in data


def test_fieldkit_version_help_flag_returns_0(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--features" in out


# ---------------------------------------------------------------------------
# --version and -V shortcuts in __main__
# ---------------------------------------------------------------------------


# ── TestVersionShortcut (flattened) ─────────────────────────────────────────


def test_fieldkit_version_double_dash_version(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fieldkit" in out


def test_fieldkit_version_dash_v_shortcut(capsys: pytest.CaptureFixture) -> None:
    rc = main(["-V"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fieldkit" in out


def test_fieldkit_version_version_group_dispatches(capsys: pytest.CaptureFixture) -> None:
    """'fieldkit version' dispatches to version.main([])."""
    rc = main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fieldkit" in out


# ---------------------------------------------------------------------------
# --features flag (human-readable)
# ---------------------------------------------------------------------------


def _features_human() -> str:
    """Run --features with stubbed issues and return output. Cached at module level."""
    import io
    from contextlib import redirect_stdout

    with patch("fieldkit.commands.version._probe_issues", return_value=_STUB_ISSUES):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--features"])
        return buf.getvalue()


def _features_json() -> dict:
    """Run --features --json with stubbed issues and return parsed dict. Cached at module level."""
    import io
    from contextlib import redirect_stdout

    with patch("fieldkit.commands.version._probe_issues", return_value=_STUB_ISSUES):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--features", "--json"])
        return json.loads(buf.getvalue())


# Module-level cache — computed once per worker process, shared across tests in the same worker.
# Each xdist worker processes a subset of tests; most workers won't need both variants.
# Uses a dict container to avoid global-statement lint (PLW0603).
_CACHE: dict[str, object] = {}


@pytest.fixture
def features_human_out() -> str:
    if "human" not in _CACHE:
        _CACHE["human"] = _features_human()
    return str(_CACHE["human"])


@pytest.fixture
def features_json() -> dict:  # type: ignore[return]
    if "json" not in _CACHE:
        _CACHE["json"] = _features_json()
    return _CACHE["json"]  # type: ignore[return-value]


# ── TestFeaturesHuman (flattened) ───────────────────────────────────────────


def test_render_features_human_returns_0() -> None:
    with patch("fieldkit.commands.version._probe_issues", return_value=_STUB_ISSUES):
        rc = main(["--features"])
    assert rc == 0


def test_render_features_human_contains_groups_section(features_human_out: str) -> None:
    assert "Groups:" in features_human_out


def test_render_features_human_contains_skills_section(features_human_out: str) -> None:
    assert "Skills:" in features_human_out


def test_render_features_human_contains_config_section(features_human_out: str) -> None:
    assert "Config:" in features_human_out


def test_render_features_human_contains_services_section(features_human_out: str) -> None:
    assert "Services:" in features_human_out


def test_render_features_human_lists_known_groups(features_human_out: str) -> None:
    for group in ("gmail", "sf", "init", "watch", "ingest"):
        assert group in features_human_out


def test_render_features_human_check_icons_present(features_human_out: str) -> None:
    """Output uses ✓/✗ check icons for service status."""
    assert "✓" in features_human_out or "✗" in features_human_out


# ---------------------------------------------------------------------------
# --features --json (machine-readable)
# ---------------------------------------------------------------------------


# ── TestFeaturesJson (flattened) ────────────────────────────────────────────


def test_collect_features_returns_0() -> None:
    with patch("fieldkit.commands.version._probe_issues", return_value=_STUB_ISSUES):
        rc = main(["--features", "--json"])
    assert rc == 0


def test_collect_features_top_level_keys(features_json: dict) -> None:
    assert "cli" in features_json
    assert "skills" in features_json
    assert "config" in features_json
    assert "services" in features_json


def test_collect_features_cli_section(features_json: dict) -> None:
    cli = features_json["cli"]
    assert "version" in cli
    assert "python" in cli
    assert "platform" in cli
    assert "arch" in cli
    assert "groups" in cli
    assert isinstance(cli["groups"], list)


def test_collect_features_groups_have_required_keys(features_json: dict) -> None:
    for g in features_json["cli"]["groups"]:
        assert "group" in g
        assert "description" in g
        assert "subcommands" in g
        assert isinstance(g["subcommands"], list)


def test_collect_features_multi_subcommand_groups_have_rich_entries(features_json: dict) -> None:
    groups = {g["group"]: g for g in features_json["cli"]["groups"]}
    gmail_subs = groups["gmail"]["subcommands"]
    assert len(gmail_subs) > 0
    for s in gmail_subs:
        assert "name" in s
        assert "description" in s


def test_collect_features_brief_group_has_open_subcommand(features_json: dict) -> None:
    groups = {g["group"]: g for g in features_json["cli"]["groups"]}
    brief = groups["brief"]
    sub_names = {s["name"] for s in brief["subcommands"]}
    assert "open" in sub_names


def test_collect_features_known_groups_present(features_json: dict) -> None:
    names = {g["group"] for g in features_json["cli"]["groups"]}
    for expected in ("gmail", "sf", "init", "watch", "ingest"):
        assert expected in names


def test_collect_features_skills_section_structure(features_json: dict) -> None:
    skills = features_json["skills"]
    assert "available" in skills
    assert "count" in skills


def test_collect_features_config_section_structure(features_json: dict) -> None:
    cfg = features_json["config"]
    assert "available" in cfg


def test_collect_features_services_section_structure(features_json: dict) -> None:
    svcs = features_json["services"]
    assert "mcpjungle" in svcs
    assert "gmail_cache" in svcs
    assert "sf_token" in svcs
    assert "chrome_debug" in svcs


def test_collect_features_service_entries_have_available_key(features_json: dict) -> None:
    for name, svc in features_json["services"].items():
        assert "available" in svc, f"service {name!r} missing 'available' key"


# ---------------------------------------------------------------------------
# Probe unit tests (isolated)
# ---------------------------------------------------------------------------


# ── TestProbeGroups (flattened) ─────────────────────────────────────────────


def test_probe_groups_returns_list() -> None:
    groups = _probe_groups()
    assert isinstance(groups, list)
    assert len(groups) > 0


def test_probe_groups_each_entry_has_required_keys() -> None:
    for g in _probe_groups():
        assert "group" in g
        assert "description" in g
        assert "subcommands" in g


def test_probe_groups_gmail_has_subcommand_list() -> None:
    groups = {g["group"]: g for g in _probe_groups()}
    subs = groups["gmail"]["subcommands"]
    assert isinstance(subs, list)
    assert len(subs) > 0
    # Each subcommand entry has name + description
    for s in subs:
        assert "name" in s
        assert "description" in s


def test_probe_groups_gmail_subcommands_include_sync() -> None:
    groups = {g["group"]: g for g in _probe_groups()}
    names = {s["name"] for s in groups["gmail"]["subcommands"]}
    assert "sync" in names
    assert "query" in names


def test_probe_groups_brief_has_open_subcommand() -> None:
    """brief is now a group with generate-on-invoke-without-subcommand and an 'open' subcommand."""
    groups = {g["group"]: g for g in _probe_groups()}
    brief = groups["brief"]
    assert isinstance(brief["subcommands"], list)
    sub_names = {s["name"] for s in brief["subcommands"]}
    assert "open" in sub_names


# ── TestProbeSkills (flattened) ─────────────────────────────────────────────


def test_probe_skills_structure() -> None:
    """The installed skill probe exposes its stable result shape."""
    result = _probe_skills()
    assert "available" in result
    assert "count" in result


def test_probe_skills_returns_dict() -> None:
    result = _probe_skills()
    assert isinstance(result, dict)


# ── TestProbeConfig (flattened) ─────────────────────────────────────────────


def test_probe_config_returns_dict() -> None:
    result = _probe_config()
    assert isinstance(result, dict)
    assert "available" in result


def test_probe_config_when_data_root_missing() -> None:
    """_probe_config returns available=False when get_fieldkit_home raises."""
    with patch("fieldkit.config.get_fieldkit_home", side_effect=Exception("not found")):
        result = _probe_config()
    assert result["available"] is False
    assert "error" in result


# ── TestProbeServices (flattened) ───────────────────────────────────────────


def test_probe_gmail_mcpjungle_available_when_responds() -> None:
    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _probe_mcpjungle()
    assert result["available"] is True


def test_probe_gmail_mcpjungle_unavailable_on_connection_error() -> None:
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        result = _probe_mcpjungle()
    assert result["available"] is False


def test_probe_gmail_chrome_debug_available_when_responds() -> None:
    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _probe_chrome_debug()
    assert result["available"] is True


def test_probe_gmail_chrome_debug_unavailable_on_error() -> None:
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        result = _probe_chrome_debug()
    assert result == {"available": False, "port": 9222, "via": None}


def test_probe_gmail_sf_token_available_when_cookie_file_has_sid(tmp_path: Path) -> None:
    """implementation change: SF auth is available when sf-cookies.json contains a valid sid."""
    import json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        json.dumps({"cookies": [{"name": "sid", "value": "tok123"}]}),
        encoding="utf-8",
    )
    with patch("fieldkit.config.get_cookie_file", return_value=cookie_file):
        result = _probe_sf_token()
    assert result["available"] is True
    assert result["method"] == "cookie"


def test_probe_gmail_sf_token_unavailable_when_cookie_file_missing(tmp_path: Path) -> None:
    """implementation change: SF auth check now uses cookie file, not SF_ACCESS_TOKEN env var."""
    nonexistent = tmp_path / "no-cookies.json"
    with patch("fieldkit.config.get_cookie_file", return_value=nonexistent):
        result = _probe_sf_token()
    assert result["available"] is False
    assert result["method"] == "cookie"


def test_probe_gmail_reports_missing_cache(tmp_path: Path) -> None:
    db = tmp_path / "gmail.db"

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db):
        result = _probe_gmail()

    assert result == {"available": False, "path": str(db)}


def test_probe_gmail_reports_empty_cache(tmp_path: Path) -> None:
    db = tmp_path / "gmail.db"
    db.touch()

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db):
        result = _probe_gmail()

    assert result == {"available": False, "path": str(db), "error": "0-byte database"}


def test_probe_gmail_reports_populated_cache(tmp_path: Path) -> None:
    db = tmp_path / "gmail.db"
    db.write_bytes(b"x" * 1024 * 1024)

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db):
        result = _probe_gmail()

    assert result == {"available": True, "path": str(db), "size_mb": 1.0}


# ---------------------------------------------------------------------------
# _probe_issues (uses GitHub API via get_github_repo())
# ---------------------------------------------------------------------------


# ── TestProbeIssues (flattened) ─────────────────────────────────────────────


def test_probe_issues_returns_counts_from_configured_github_repo() -> None:
    """_probe_issues returns available=True and counts from gh subprocess calls."""
    import json

    open_bugs_response = MagicMock()
    open_bugs_response.returncode = 0
    open_bugs_response.stdout = json.dumps([{"number": 1}, {"number": 2}])

    open_enhs_response = MagicMock()
    open_enhs_response.returncode = 0
    open_enhs_response.stdout = json.dumps([{"number": 3}])

    closed_bugs_response = MagicMock()
    closed_bugs_response.returncode = 0
    closed_bugs_response.stdout = json.dumps([{"number": 4}, {"number": 5}, {"number": 6}])

    closed_enhs_response = MagicMock()
    closed_enhs_response.returncode = 0
    closed_enhs_response.stdout = json.dumps([])

    with (
        patch("fieldkit.config.get_github_repo", return_value="owner/repo"),
        patch(
            "subprocess.run",
            side_effect=[
                open_bugs_response,
                open_enhs_response,
                closed_bugs_response,
                closed_enhs_response,
            ],
        ),
    ):
        result = _probe_issues()

    assert result["available"] is True
    assert result["open_bugs"] == 2
    assert result["open_enhancements"] == 1
    assert result["closed"] == 3


def test_probe_issues_returns_available_false_when_issues_dir_not_configured() -> None:
    from fieldkit.config import ConfigError

    with patch(
        "fieldkit.config.get_github_repo",
        side_effect=ConfigError("github_repo not configured in config.yaml"),
    ):
        result = _probe_issues()

    assert result["available"] is False
    assert "github_repo" in result.get("error", "").lower()
    assert result["open_bugs"] == 0
    assert result["open_enhancements"] == 0


def test_probe_issues_does_not_use_get_fieldkit_root_on_config_error() -> None:
    """Must not fall back to get_fieldkit_root() on config error."""
    from fieldkit.config import ConfigError

    with (
        patch(
            "fieldkit.config.get_github_repo",
            side_effect=ConfigError("not configured"),
        ),
        patch("fieldkit.config.get_fieldkit_root") as mock_root,
    ):
        result = _probe_issues()

    mock_root.assert_not_called()
    assert result["available"] is False
