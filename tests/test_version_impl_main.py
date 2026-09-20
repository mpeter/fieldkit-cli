"""Tests for fieldkit.commands.version._impl.main and _sub_entry.

_impl.main is shadowed by fieldkit.commands.version.main (a near-duplicate
kept there so patch("fieldkit.commands.version.Path", ...) intercepts the
chrome-debug probe — see the design note in __init__.py). Nothing in the
public API calls _impl.main, so it is exercised here by importing _impl
directly. Likewise _sub_entry's dict-style SUBCOMMANDS branch has no
production caller left (no command module defines SUBCOMMANDS as a dict
any more) — it is legacy-format handling exercised only here.
"""

import importlib.metadata
import json
from unittest.mock import patch

import pytest

from fieldkit.commands.version import _impl

pytestmark = pytest.mark.unit

_STUB_FEATURES = {
    "cli": {"version": "1.2.3", "python": "3.11.9", "platform": "TestOS", "arch": "testarch", "groups": ["g1"]},
    "skills": {"available": True, "count": 1},
    "config": {"available": True},
    "services": {"mcpjungle": {"available": True}},
    "issues": {"available": True},
}


# ---------------------------------------------------------------------------
# Distribution version authority
# ---------------------------------------------------------------------------


def test_fieldkit_version_reads_fieldkit_cli_distribution_metadata() -> None:
    with patch.object(importlib.metadata, "version", return_value="1.0.0") as version:
        result = _impl._fieldkit_version()

    assert result == "1.0.0"
    version.assert_called_once_with("fieldkit-cli")


def test_fieldkit_version_uses_dev_fallback_without_distribution_metadata() -> None:
    with patch.object(
        importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError,
    ):
        result = _impl._fieldkit_version()

    assert result == "dev"


# ---------------------------------------------------------------------------
# _sub_entry
# ---------------------------------------------------------------------------


def test_sub_entry_tuple_uses_index_one_not_index_zero() -> None:
    result = _impl._sub_entry("sync", ("indexZero", "indexOne"))
    assert result == {"name": "sync", "description": "indexOne"}


def test_sub_entry_string_value_used_directly() -> None:
    result = _impl._sub_entry("sync", "plain description")
    assert result == {"name": "sync", "description": "plain description"}


def test_sub_entry_short_tuple_falls_back_to_empty_description() -> None:
    """A 1-tuple fails the len(sub_val) >= 2 guard and isn't a str either."""
    result = _impl._sub_entry("sync", ("only-one-item",))
    assert result == {"name": "sync", "description": ""}


def test_sub_entry_non_tuple_non_str_falls_back_to_empty_description() -> None:
    result = _impl._sub_entry("sync", 42)
    assert result == {"name": "sync", "description": ""}


# ---------------------------------------------------------------------------
# main — --help short-circuits everything else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_main_help_flag_short_circuits_before_any_probing(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(_impl, "_collect_features") as mock_collect,
        patch.object(_impl, "_render_version_human") as mock_render_version,
    ):
        rc = _impl.main([flag, "--features", "--json"])

    assert rc == 0
    mock_collect.assert_not_called()
    mock_render_version.assert_not_called()
    out = capsys.readouterr().out
    assert out.strip() == _impl._HELP.strip()


# ---------------------------------------------------------------------------
# main — plain (no flags): version-only path, no feature collection
# ---------------------------------------------------------------------------


def test_main_plain_renders_version_only_and_skips_feature_collection() -> None:
    with (
        patch.object(_impl, "_render_version_human") as mock_render_version,
        patch.object(_impl, "_collect_features") as mock_collect,
    ):
        rc = _impl.main([])

    assert rc == 0
    mock_render_version.assert_called_once_with()
    mock_collect.assert_not_called()


# ---------------------------------------------------------------------------
# main — --features (human render, no JSON)
# ---------------------------------------------------------------------------


def test_main_features_without_json_renders_human_not_json() -> None:
    with (
        patch.object(_impl, "_collect_features", return_value=dict(_STUB_FEATURES)) as mock_collect,
        patch.object(_impl, "_render_features_human") as mock_render_features,
    ):
        rc = _impl.main(["--features"])

    assert rc == 0
    mock_collect.assert_called_once_with()
    mock_render_features.assert_called_once_with(_STUB_FEATURES)


def test_main_features_short_flag_dash_f_also_triggers_features() -> None:
    with (
        patch.object(_impl, "_collect_features", return_value=dict(_STUB_FEATURES)),
        patch.object(_impl, "_render_features_human") as mock_render_features,
    ):
        rc = _impl.main(["-f"])

    assert rc == 0
    mock_render_features.assert_called_once_with(_STUB_FEATURES)


# ---------------------------------------------------------------------------
# main — --features --json (raw JSON dump, no human render)
# ---------------------------------------------------------------------------


def test_main_features_with_json_echoes_raw_json_and_skips_human_render(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(_impl, "_collect_features", return_value=dict(_STUB_FEATURES)),
        patch.object(_impl, "_render_features_human") as mock_render_features,
    ):
        rc = _impl.main(["--features", "--json"])

    assert rc == 0
    mock_render_features.assert_not_called()
    out = capsys.readouterr().out
    assert json.loads(out) == _STUB_FEATURES


# ---------------------------------------------------------------------------
# main — --json alone: flattens cli section to top level, pops "cli"
# ---------------------------------------------------------------------------


def test_main_json_without_features_flattens_cli_section_to_top_level(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(_impl, "_collect_features", return_value=dict(_STUB_FEATURES)):
        rc = _impl.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "1.2.3"
    assert payload["python"] == "3.11.9"
    assert payload["platform"] == "TestOS"
    assert payload["arch"] == "testarch"
    assert payload["groups"] == ["g1"]
    assert "cli" not in payload
    assert payload["skills"] == {"available": True, "count": 1}
    assert payload["services"] == {"mcpjungle": {"available": True}}


def test_main_json_without_features_falls_back_when_cli_section_absent(capsys: pytest.CaptureFixture[str]) -> None:
    stub_no_cli = {"skills": {}, "config": {}, "services": {}, "issues": {}}
    with patch.object(_impl, "_collect_features", return_value=stub_no_cli):
        rc = _impl.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == _impl._fieldkit_version()
    assert payload["groups"] == []
