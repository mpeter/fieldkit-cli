"""Unit tests for misplaced ShadowBot configuration key warnings (#95)."""

import logging
from collections.abc import Mapping
from unittest.mock import patch

import pytest

from fieldkit.config import clear_config_caches, get_shadowbot_config_warnings, log_shadowbot_config_warnings_once
from fieldkit.config._shadowbot import shadowbot_config_warnings

pytestmark = pytest.mark.unit

_LOADER_MODULE = "fieldkit.config._loader"


@pytest.mark.parametrize(
    ("data", "expected_fragments"),
    [
        pytest.param(
            {"shadowbot_chrome_cookies_path": "Profile 2/Cookies"},
            ["shadowbot_chrome_cookies_path", "shadowbot.chrome_cookies_path"],
            id="top-level-known-key",
        ),
        pytest.param(
            {"shadowbot": {"chrome_cookie_path": "Cookies"}},
            ["resembles 'chrome_cookies_path'", "not a recognized"],
            id="misspelled-in-section",
        ),
        pytest.param(
            {"shadowbot_client_idd": 1},
            ["resembles 'client_id'", "not a recognized"],
            id="top-level-near-miss-suffix",
        ),
        pytest.param({"fieldkit_home": "home", "other_key": 1}, [], id="unrelated-top-level"),
        pytest.param(
            {"shadowbot": {"chrome_cookies_path": "Cookies", "chrome_recovery": True, "client_id": "c"}},
            [],
            id="correct",
        ),
        pytest.param(
            {"shadowbot": {"chrome_cookies_path": "Cookies"}},
            ["chrome_cookies_path is set but Chrome recovery is off", "shadowbot.chrome_recovery: true"],
            id="cookie-path-without-opt-in",
        ),
        pytest.param({"shadowbot": {"redirect_uri": "https://example.com/cb"}}, [], id="redirect-uri-is-known"),
        pytest.param({"shadowbot_token": "tokens/shadowbot.json"}, [], id="honored-token-key"),
        pytest.param({"shadowbot_assistant_id": "a"}, ["shadowbot_assistant_id"], id="normal-key-still-named"),
        pytest.param({}, [], id="absent-section"),
        pytest.param({"shadowbot": "not-a-mapping"}, [], id="non-mapping-section"),
    ],
)
def test_shadowbot_config_warnings(data: Mapping[str, object], expected_fragments: list[str]) -> None:
    """Each ignored ShadowBot key must produce exactly one advisory, and valid configuration none."""
    warnings = shadowbot_config_warnings(data)

    assert isinstance(warnings, tuple)
    assert len(warnings) == (1 if expected_fragments else 0)
    for fragment in expected_fragments:
        assert fragment in warnings[0]


_SENSITIVE_KEYS = [
    pytest.param("alice@example.com", id="email-like"),
    pytest.param("bad\nWARNING forged log line", id="newline"),
    pytest.param("k" * 65, id="over-long"),
    pytest.param("alice_smith", id="identifier-shaped-name"),
    pytest.param("chrome_cookies_path_alice", id="near-miss-with-identifier"),
]


@pytest.mark.parametrize("key", _SENSITIVE_KEYS)
@pytest.mark.parametrize("location", ["section", "top-level"])
def test_unsafe_config_keys_are_not_echoed(key: str, location: str) -> None:
    """Config keys are user text that may carry identifiers or forged log lines, so unrelated names stay out of warnings."""
    data: Mapping[str, object] = {"shadowbot": {key: 1}} if location == "section" else {f"shadowbot_{key}": 1}

    warnings = shadowbot_config_warnings(data)

    assert len(warnings) == 1
    assert "nrecognized" in warnings[0]
    assert key not in warnings[0]
    assert key.split("\n")[0] not in warnings[0]


@pytest.mark.parametrize("key", [123, None], ids=["int", "none"])
def test_non_string_section_keys_get_the_generic_warning(key: object) -> None:
    """YAML allows non-string keys; they are ignored by fieldkit, so the user must still be told."""
    warnings = shadowbot_config_warnings({"shadowbot": {key: 1}})

    assert len(warnings) == 1
    assert "An unrecognized key under 'shadowbot:'" in warnings[0]
    assert "resembles" not in warnings[0]


def test_near_miss_names_only_the_schema_field() -> None:
    """A typo is explained by the schema field it resembles, so no user text reaches the warning."""
    warnings = shadowbot_config_warnings({"shadowbot": {"chrome_cookies_path_alice": 1}})

    assert len(warnings) == 1
    assert "resembles 'chrome_cookies_path'" in warnings[0]
    assert "alice" not in warnings[0]


def test_unsafe_config_key_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The log sink is where a leaked key would persist, so it gets the same redaction guarantee as doctor output."""
    clear_config_caches()
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot": {"alice@example.com": 1}}),
        caplog.at_level(logging.WARNING),
    ):
        log_shadowbot_config_warnings_once()

    assert "nrecognized" in caplog.text
    assert "alice@example.com" not in caplog.text


def test_get_shadowbot_config_warnings_reads_loaded_config() -> None:
    """Doctor reads warnings from the loaded configuration rather than from a separate parse."""
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot_client_id": "c"}):
        warnings = get_shadowbot_config_warnings()

    assert len(warnings) == 1
    assert "shadowbot.client_id" in warnings[0]


def test_get_shadowbot_config_warnings_without_config_is_empty() -> None:
    """A missing config file is normal and must not be reported as a problem."""
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_config_warnings() == ()


def test_log_shadowbot_config_warnings_once_logs_a_single_record(caplog: pytest.LogCaptureFixture) -> None:
    """Repeated auth attempts must not repeat the same warning in the logs."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot_chrome_cookies_path": "x"}),
        caplog.at_level(logging.WARNING),
    ):
        first = log_shadowbot_config_warnings_once()
        log_shadowbot_config_warnings_once()

    assert first is None
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "shadowbot.chrome_cookies_path" in records[0].getMessage()


def test_clear_config_caches_rearms_the_once_log(caplog: pytest.LogCaptureFixture) -> None:
    """Clearing config caches must re-arm the once-only log so reloaded configuration is reported again."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot": {"typo": 1}}),
        caplog.at_level(logging.WARNING),
    ):
        log_shadowbot_config_warnings_once()
        clear_config_caches()
        log_shadowbot_config_warnings_once()

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
