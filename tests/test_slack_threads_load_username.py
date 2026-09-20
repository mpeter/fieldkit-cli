"""Unit tests for load_current_username in fieldkit.watch.slack_threads.

CRAP cleanup target — load_current_username had CRAP=44.83 (complexity=9)
with only the SLACK_USERNAME env-var guard covered. These tests exercise
every branch: the env-var short-circuit, the missing-config early return,
the OSError/YAMLError read failures, the not-a-dict guard on the loaded
YAML, the identity.slack_username-or-identity.username extraction (with its
own isinstance(identity, dict) guard), and the getpass.getuser() system
fallback.

Per watch/AGENTS.md's L01 gotcha, `_identity_config()` is `@cache`-decorated
and its cache is NOT cleared by conftest's `_clear_watcher_dir_caches()`
autouse fixture. Patching `get_fieldkit_home` would not be picked up once
any test in the session has already called `_identity_config()`. Instead,
every test here patches `_identity_config` directly at the module-level
binding: `fieldkit.watch.slack_threads._identity_config`.
"""

from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest

from fieldkit.watch.slack_threads import load_current_username

pytestmark = pytest.mark.unit


def _make_yaml_file(tmp_path: Path, content: str) -> Path:
    """Write `content` to a real identity.yaml under tmp_path and return its path."""
    path = tmp_path / "identity.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _make_missing_path(tmp_path: Path) -> Path:
    """Return a Path under tmp_path that does not exist."""
    return tmp_path / "identity.yaml"


# ---------------------------------------------------------------------------
# 1. SLACK_USERNAME env var short-circuit
# ---------------------------------------------------------------------------


def test_env_var_set_returns_stripped_lowered_without_touching_identity_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_USERNAME", "  Bob.Smith  ")
    with patch("fieldkit.watch.slack_threads._identity_config") as mock_identity_config:
        result = load_current_username()

    assert result == "bob.smith"
    mock_identity_config.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Identity config missing entirely
# ---------------------------------------------------------------------------


def test_missing_identity_config_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    missing_path = _make_missing_path(tmp_path)
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=missing_path):
        result = load_current_username()

    assert result is None


# ---------------------------------------------------------------------------
# 3. File read raises OSError
# ---------------------------------------------------------------------------


def test_identity_config_read_oserror_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(tmp_path, "identity:\n  username: irrelevant\n")

    class _ExplodingPath(type(config_path)):  # type: ignore[misc]
        def open(self, *args: object, **kwargs: object) -> NoReturn:  # type: ignore[override]
            raise OSError("boom")

    exploding_path = _ExplodingPath(config_path)
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=exploding_path):
        result = load_current_username()

    assert result is None


# ---------------------------------------------------------------------------
# 4. yaml.safe_load raises YAMLError (malformed YAML)
# ---------------------------------------------------------------------------


def test_malformed_yaml_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    # Unbalanced flow mapping — triggers yaml.YAMLError on parse.
    config_path = _make_yaml_file(tmp_path, "identity: [unclosed\n")
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path):
        result = load_current_username()

    assert result is None


# ---------------------------------------------------------------------------
# 5. Parsed YAML is not a dict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "yaml_content",
    [
        "- one\n- two\n",
        "just a string\n",
        "",
    ],
    ids=["list", "bare-string", "null"],
)
def test_non_dict_yaml_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_content: str) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(tmp_path, yaml_content)
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path):
        result = load_current_username()

    assert result is None


# ---------------------------------------------------------------------------
# 6. identity.slack_username present (and prioritized over identity.username)
# ---------------------------------------------------------------------------


def test_identity_slack_username_takes_priority_over_username(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(
        tmp_path,
        "identity:\n  slack_username: '  Alice.Jones  '\n  username: someone-else\n",
    )
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path):
        result = load_current_username()

    assert result == "alice.jones"


# ---------------------------------------------------------------------------
# 7. `or` fallback — both operands proven independently
# ---------------------------------------------------------------------------


def test_identity_username_used_when_slack_username_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(tmp_path, "identity:\n  username: '  Carol  '\n")
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path):
        result = load_current_username()

    assert result == "carol"


def test_identity_username_used_when_slack_username_falsy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(
        tmp_path,
        "identity:\n  slack_username: ''\n  username: '  Dave  '\n",
    )
    with patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path):
        result = load_current_username()

    assert result == "dave"


# ---------------------------------------------------------------------------
# 8. identity key present but not a dict -> falls through to system fallback
# ---------------------------------------------------------------------------


def test_identity_not_a_dict_falls_through_to_system_username(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(tmp_path, "identity: 'not-a-mapping'\n")
    with (
        patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path),
        patch("getpass.getuser", return_value="SystemUser"),
    ):
        result = load_current_username()

    assert result == "systemuser"


# ---------------------------------------------------------------------------
# 9. No usable identity data at all -> getpass.getuser() system fallback
# ---------------------------------------------------------------------------


def test_no_usable_identity_falls_back_to_getpass_getuser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SLACK_USERNAME", raising=False)
    config_path = _make_yaml_file(tmp_path, "other_key: value\n")
    with (
        patch("fieldkit.watch.slack_threads._identity_config", return_value=config_path),
        patch("getpass.getuser", return_value="FallbackUser"),
    ):
        result = load_current_username()

    assert result == "fallbackuser"
