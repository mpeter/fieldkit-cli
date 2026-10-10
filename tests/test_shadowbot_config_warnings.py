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
            ["shadowbot.chrome_cookie_path", "not a recognized"],
            id="misspelled-in-section",
        ),
        pytest.param(
            {"shadowbot_unknown": 1},
            ["shadowbot_unknown", "not a recognized"],
            id="top-level-unknown-suffix",
        ),
        pytest.param({"fieldkit_home": "home", "other_key": 1}, [], id="unrelated-top-level"),
        pytest.param({"shadowbot": {"chrome_cookies_path": "Cookies", "client_id": "c"}}, [], id="correct"),
        pytest.param({"shadowbot": {"redirect_uri": "https://example.com/cb"}}, [], id="redirect-uri-is-known"),
        pytest.param({}, [], id="absent-section"),
        pytest.param({"shadowbot": "not-a-mapping"}, [], id="non-mapping-section"),
    ],
)
def test_shadowbot_config_warnings(data: Mapping[str, object], expected_fragments: list[str]) -> None:
    warnings = shadowbot_config_warnings(data)

    assert isinstance(warnings, tuple)
    assert len(warnings) == (1 if expected_fragments else 0)
    for fragment in expected_fragments:
        assert fragment in warnings[0]


def test_get_shadowbot_config_warnings_reads_loaded_config() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot_client_id": "c"}):
        warnings = get_shadowbot_config_warnings()

    assert len(warnings) == 1
    assert "shadowbot.client_id" in warnings[0]


def test_get_shadowbot_config_warnings_without_config_is_empty() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_config_warnings() == ()


def test_log_shadowbot_config_warnings_once_logs_a_single_record(caplog: pytest.LogCaptureFixture) -> None:
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
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot": {"typo": 1}}),
        caplog.at_level(logging.WARNING),
    ):
        log_shadowbot_config_warnings_once()
        clear_config_caches()
        log_shadowbot_config_warnings_once()

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
