"""Tests for fieldkit.config — ConfigError and get_fieldkit_home()."""

import json
import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

import fieldkit.config
import fieldkit.config._accounts as accounts_config
import fieldkit.config._integrations as integrations_config
import fieldkit.config._loader
from fieldkit.config import (
    ConfigError,
    clear_config_caches,
    get_accounts_root,
    get_config_path,
    get_email_domain,
    get_fieldkit_data,
    get_fieldkit_home,
    get_harness_scratch_root,
    get_integration_configuration_state,
    get_sf_rest_base_url,
    get_sf_session_id,
)
from fieldkit.config._loader import _CACHED

pytestmark = pytest.mark.unit


# ── TestImportSmoke (flattened) ─────────────────────────────────────────────


def test_import_smoke_import() -> None:
    """get_fieldkit_home and ConfigError are importable from fieldkit.config."""
    assert callable(get_fieldkit_home)
    assert issubclass(ConfigError, Exception)


def test_suite_config_is_isolated_from_operator_environment() -> None:
    """The session config lives under the temporary HOME established by conftest."""
    config_path = fieldkit.config._loader.CONFIG_PATH

    assert config_path == fieldkit.config.CONFIG_PATH
    assert config_path.is_file()
    assert config_path.parent.parent.parent == Path(os.environ["HOME"])
    assert get_fieldkit_home() == Path(os.environ["HOME"]) / "fieldkit_home"


# ── TestGetFieldkitHome (flattened) ─────────────────────────────────────────


def test_get_fieldkit_home_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fieldkit-data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_fieldkit_home()

    assert isinstance(result, Path)
    assert result.is_absolute()
    assert result == Path("/tmp/fieldkit-data")


def test_get_fieldkit_home_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: ~/work/fieldkit-data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_fieldkit_home()

    assert isinstance(result, Path)
    assert result.is_absolute()
    assert result == Path("~/work/fieldkit-data").expanduser().resolve()


def test_get_fieldkit_home_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", missing)

    with pytest.raises(ConfigError, match="not found"):
        get_fieldkit_home()


def test_get_fieldkit_home_malformed_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: [bad\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.raises(ConfigError, match=r"Invalid YAML"):
        get_fieldkit_home()


def test_get_fieldkit_home_missing_data_repo_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("other_key: some_value\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.raises(ConfigError, match="fieldkit_home"):
        get_fieldkit_home()


def test_get_fieldkit_home_non_dict_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("just a plain string\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.raises(ConfigError, match=r"must contain a YAML mapping"):
        get_fieldkit_home()


def test_get_fieldkit_home_missing_fieldkit_home_key_includes_migration_hint(tmp_path: Path) -> None:
    """ConfigError when fieldkit_home absent includes migration hint."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("other_key: value\n", encoding="utf-8")
    with (
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ConfigError, match=r"data_repo.*renamed.*fieldkit_home"),
    ):
        get_fieldkit_home()


# ── TestGetFieldkitData (flattened) ─────────────────────────────────────────


def test_get_fieldkit_data_get_fieldkit_data_explicit_key(tmp_path: Path) -> None:
    """When fieldkit_data is explicitly set, that path is returned."""
    data_dir = tmp_path / "mydata"
    data_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {data_dir}\n",
        encoding="utf-8",
    )
    with patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        result = get_fieldkit_data()
    assert result == data_dir.resolve()


def test_get_fieldkit_data_get_fieldkit_data_defaults_to_fieldkit_home_slash_data(tmp_path: Path) -> None:
    """When fieldkit_data is absent, defaults to fieldkit_home/data."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    with patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        result = get_fieldkit_data()
    assert result == (tmp_path / "data").resolve()


def test_get_fieldkit_data_get_fieldkit_data_defaults_when_load_raw_config_returns_none(tmp_path: Path) -> None:
    """When _load_raw_config returns None, defaults to fieldkit_home/data."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    with (
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        patch("fieldkit.config._loader._load_raw_config", return_value=None),
    ):
        result = get_fieldkit_data()
    assert result == (tmp_path / "data").resolve()


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_get_fieldkit_data_get_fieldkit_data_empty_value_raises_config_error(tmp_path: Path, bad_value: str) -> None:
    """Empty-string or whitespace-only fieldkit_data raises ConfigError."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {bad_value!r}\n",
        encoding="utf-8",
    )
    with (
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ConfigError, match=r"fieldkit_data"),
    ):
        get_fieldkit_data()


def test_get_fieldkit_data_get_fieldkit_data_cache_cleared_by_clear_config_caches(tmp_path: Path) -> None:
    """clear_config_caches() clears get_fieldkit_data cache."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    with patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        clear_config_caches()
        first = get_fieldkit_data()
        # Change config and clear caches — second call should reflect new config
        new_data = tmp_path / "newdata"
        new_data.mkdir()
        cfg.write_text(
            f"fieldkit_home: {tmp_path}\nfieldkit_data: {new_data}\n",
            encoding="utf-8",
        )
        clear_config_caches()
        second = get_fieldkit_data()
    assert first != second
    assert second == new_data.resolve()


def test_get_fieldkit_data_fieldkit_home_absent_fieldkit_data_absent_raises_config_error(tmp_path: Path) -> None:
    """When fieldkit_data key absent, falls back to get_fieldkit_home() which raises ConfigError."""
    cfg = tmp_path / "config.yaml"
    # Only other_key present — neither fieldkit_home nor fieldkit_data
    cfg.write_text("other_key: value\n", encoding="utf-8")
    with (
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ConfigError, match="fieldkit_home"),
    ):
        get_fieldkit_data()  # no fieldkit_data key → falls back to get_fieldkit_home() / "data"


def test_get_fieldkit_data_respects_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FIELDKIT_DATA_DIR env var overrides config-based resolution."""
    env_dir = tmp_path / "isolated-data"
    env_dir.mkdir()
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(env_dir))
    # Clear any cached result from prior tests.
    clear_config_caches()
    result = get_fieldkit_data()
    assert result == env_dir.resolve()


# ---------------------------------------------------------------------------
# get_harness_scratch_root — cache-class root for ephemeral harness worktrees
# (historic regression). Not get_fieldkit_data(); never cached.
# ---------------------------------------------------------------------------


def test_get_harness_scratch_root_respects_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FIELDKIT_HARNESS_ROOT overrides XDG/default resolution (driver sets it per run)."""
    env_dir = tmp_path / "scratch"
    env_dir.mkdir()
    monkeypatch.setenv("FIELDKIT_HARNESS_ROOT", str(env_dir))

    result = get_harness_scratch_root()

    assert result == env_dir.resolve()


def test_get_harness_scratch_root_defaults_to_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override, the root is XDG_CACHE_HOME/fieldkit."""
    monkeypatch.delenv("FIELDKIT_HARNESS_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    result = get_harness_scratch_root()

    assert result == (tmp_path / "fieldkit").resolve()


def test_get_harness_scratch_root_defaults_to_dot_cache_without_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither override nor XDG_CACHE_HOME, the root is ~/.cache/fieldkit."""
    monkeypatch.delenv("FIELDKIT_HARNESS_ROOT", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    result = get_harness_scratch_root()

    assert result == (Path.home() / ".cache" / "fieldkit").resolve()


def test_get_harness_scratch_root_relative_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-absolute FIELDKIT_HARNESS_ROOT is a config error, not silently resolved against cwd."""
    monkeypatch.setenv("FIELDKIT_HARNESS_ROOT", "relative/scratch")

    with pytest.raises(ConfigError, match="must be an absolute path"):
        get_harness_scratch_root()


def test_driver_and_health_worktree_roots_share_the_harness_scratch_root() -> None:
    """historic regression/historic regression: both subsystems resolve under one root — they must not fork."""
    from fieldkit.driver.runner import _worktrees_root as driver_root
    from fieldkit.health.runner import _worktrees_root as health_root

    base = get_harness_scratch_root()

    assert driver_root().is_relative_to(base)
    assert health_root().is_relative_to(base)
    assert driver_root() != health_root()


def test_get_fieldkit_data_falls_through_when_env_var_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without FIELDKIT_DATA_DIR, get_fieldkit_data falls through to config."""
    monkeypatch.delenv("FIELDKIT_DATA_DIR", raising=False)
    clear_config_caches()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {home_dir}\n")
    with patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        result = get_fieldkit_data()
    assert result == (home_dir / "data").resolve()


# ── TestHelperPaths (flattened) ─────────────────────────────────────────────


def test_helper_paths_get_accounts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fieldkit-data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_accounts_root()

    assert result == Path("/tmp/fieldkit-data/accounts")


def test_helper_paths_get_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fieldkit-data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_config_path("accounts.yaml")

    assert result == Path("/tmp/fieldkit-data/config/accounts.yaml")


def _write_cookie_json(path: Path, cookies: list[dict]) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies}, f)


# ── TestGetSfSessionId (flattened) ──────────────────────────────────────────


def test_get_sf_session_id_missing_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sf_session_id returns None when cookie file does not exist."""
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(integrations_config, "get_cookie_file", lambda: missing)
    # Re-import to pick up the monkeypatch (function reads get_cookie_file() at call time)
    from fieldkit.config import get_sf_session_id as _fn

    result = _fn()
    assert result is None


def test_get_sf_session_id_happy_path_returns_sid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sf_session_id returns the sid value from a my.salesforce.com cookie."""
    cookie_file = tmp_path / "sf-cookies.json"
    _write_cookie_json(
        cookie_file,
        [
            {
                "name": "sid",
                "value": "EXPECTED_SID_VALUE",
                "domain": "examplecrm.my.salesforce.com",
                "path": "/",
            },
            {
                "name": "oid",
                "value": "ORG_ID",
                "domain": "examplecrm.my.salesforce.com",
                "path": "/",
            },
        ],
    )
    monkeypatch.setattr(integrations_config, "get_cookie_file", lambda: cookie_file)

    result = get_sf_session_id()

    assert result == "EXPECTED_SID_VALUE"


def test_get_sf_session_id_wrong_domain_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sf_session_id returns None when sid is only on .force.com (Lightning sid)."""
    cookie_file = tmp_path / "sf-cookies.json"
    _write_cookie_json(
        cookie_file,
        [
            {
                "name": "sid",
                "value": "LIGHTNING_SID",
                "domain": ".force.com",
                "path": "/",
            },
            {
                "name": "sid",
                "value": "ANOTHER_SID",
                "domain": "examplecrm.lightning.force.com",
                "path": "/",
            },
        ],
    )
    monkeypatch.setattr(integrations_config, "get_cookie_file", lambda: cookie_file)

    result = get_sf_session_id()

    assert result is None


def test_get_sf_session_id_no_sid_cookie_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sf_session_id returns None when there is no sid-named cookie at all."""
    cookie_file = tmp_path / "sf-cookies.json"
    _write_cookie_json(
        cookie_file,
        [
            {
                "name": "oid",
                "value": "ORG_ID",
                "domain": "examplecrm.my.salesforce.com",
                "path": "/",
            },
            {
                "name": "csrfToken",
                "value": "CSRF",
                "domain": "examplecrm.my.salesforce.com",
                "path": "/",
            },
        ],
    )
    monkeypatch.setattr(integrations_config, "get_cookie_file", lambda: cookie_file)

    result = get_sf_session_id()

    assert result is None


def test_get_sf_session_id_bad_json_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sf_session_id returns None when the cookie file contains invalid JSON."""
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text("not valid json {{{{", encoding="utf-8")
    monkeypatch.setattr(integrations_config, "get_cookie_file", lambda: cookie_file)

    result = get_sf_session_id()

    assert result is None


# ── Test_sf_rest_base_url (flattened) ───────────────────────────────────────


def test_get_sf_rest_base_url_lightning_url_transforms_to_my_salesforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal Lightning URL is transformed to my.salesforce.com."""
    monkeypatch.setattr(
        accounts_config,
        "get_salesforce_org_url",
        lambda: "https://examplecrm.lightning.force.com",
    )
    assert get_sf_rest_base_url() == "https://examplecrm.my.salesforce.com"


def test_get_sf_rest_base_url_already_my_salesforce_url_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL already in my.salesforce.com form is returned as-is."""
    monkeypatch.setattr(
        accounts_config,
        "get_salesforce_org_url",
        lambda: "https://examplecrm.my.salesforce.com",
    )
    assert get_sf_rest_base_url() == "https://examplecrm.my.salesforce.com"


def test_get_sf_rest_base_url_empty_org_url_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty org_url yields an empty string."""
    monkeypatch.setattr(accounts_config, "get_salesforce_org_url", lambda: "")
    assert get_sf_rest_base_url() == ""


def test_get_sf_rest_base_url_unrecognised_url_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL without .force.com or .salesforce.com raises ConfigError (spec 043 G1b).

    Previously returned ""; now raises ConfigError so callers get a clear
    error instead of a misleading connect error at API call time.
    """
    monkeypatch.setattr(
        accounts_config,
        "get_salesforce_org_url",
        lambda: "https://example.com/sfdc",
    )
    with pytest.raises(ConfigError, match=r"Unrecognised"):
        get_sf_rest_base_url()


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.my.salesforce.com",
        "https://acme.salesforce.com.attacker.example",
        "https://acme.my.salesforce.com/path",
        "https://user@" + "acme.my.salesforce.com",
    ],
)
def test_get_sf_rest_base_url_rejects_unsafe_url_shapes(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setattr(accounts_config, "get_salesforce_org_url", lambda: url)

    with pytest.raises(ConfigError, match="Unrecognised"):
        get_sf_rest_base_url()


def test_get_sf_rest_base_url_trailing_slash_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trailing slash in org_url is stripped before transformation."""
    monkeypatch.setattr(
        accounts_config,
        "get_salesforce_org_url",
        lambda: "https://examplecrm.lightning.force.com/",
    )
    assert get_sf_rest_base_url() == "https://examplecrm.my.salesforce.com"


def test_sf_integration_state_uses_canonical_resolver() -> None:
    with patch("fieldkit.config._integrations.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"):
        result = get_integration_configuration_state("sf")

    assert result == "enabled"


def test_sf_integration_state_rejects_invalid_url() -> None:
    with patch("fieldkit.config._integrations.get_sf_rest_base_url", side_effect=ConfigError("invalid URL")):
        result = get_integration_configuration_state("sf")

    assert result == "invalid"


def test_get_integration_configuration_state_rejects_unreadable_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.mkdir()
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_path)
    clear_config_caches()

    try:
        result = get_integration_configuration_state("google")
    finally:
        clear_config_caches()

    assert result == "invalid"


@pytest.mark.parametrize(
    ("data", "client_id", "client_secret", "expected"),
    [
        ({"gmail_token": "/tmp/token.json"}, None, None, "enabled"),
        ({"gmail_token": ""}, None, None, "invalid"),
        ({}, "client", None, "invalid"),
        ({}, "client", "secret", "enabled"),
        ({}, None, None, "disabled"),
    ],
)
def test_get_integration_configuration_state_classifies_google_configuration(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, object],
    client_id: str | None,
    client_secret: str | None,
    expected: str,
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    if client_id is not None:
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", client_id)
    if client_secret is not None:
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", client_secret)

    with patch("fieldkit.config._loader._load_raw_config", return_value=data):
        result = get_integration_configuration_state("google")

    assert result == expected


@pytest.mark.parametrize(
    ("shadowbot", "expected"),
    [
        (None, "disabled"),
        ({}, "invalid"),
        ({"api_base": "https://api.example.com"}, "invalid"),
        (
            {
                "api_base": "https://api.example.com",
                "token_endpoint": "https://auth.example.com/token",
                "auth_endpoint": "https://auth.example.com/authorize",
            },
            "enabled",
        ),
    ],
)
def test_get_integration_configuration_state_classifies_shadowbot_configuration(
    shadowbot: object, expected: str
) -> None:
    with patch("fieldkit.config._loader._load_raw_config", return_value={"shadowbot": shadowbot}):
        result = get_integration_configuration_state("shadowbot")

    assert result == expected


# ── TestGetPipelineQuota (flattened) ────────────────────────────────────────


def test_get_pipeline_quota_returns_none_when_config_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", tmp_path / "config.yaml")
    assert fieldkit.config.get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_pipeline_section_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp/data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    assert fieldkit.config.get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_quota_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp/data\npipeline:\n  other: 1\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    assert fieldkit.config.get_pipeline_quota() is None


def test_get_pipeline_quota_returns_dict_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fieldkit_home: /tmp/data\npipeline:\n  quota:\n    target: 5000000\n    period: 2026-H2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config.get_pipeline_quota()
    assert result == {"target": 5000000, "period": "2026-H2"}


# ── TestWritePipelineQuota (flattened) ──────────────────────────────────────


def test_write_pipeline_quota_writes_quota_to_new_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    fieldkit.config.write_pipeline_quota(3000000, "2026-H1")
    assert cfg.exists()
    result = fieldkit.config.get_pipeline_quota()
    assert result == {"target": 3000000, "period": "2026-H1"}


def test_write_pipeline_quota_round_trip_preserves_existing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp/data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    fieldkit.config.write_pipeline_quota(5000000, "2026-H2")
    result = fieldkit.config.get_pipeline_quota()
    assert result == {"target": 5000000, "period": "2026-H2"}
    # Existing key preserved
    import yaml

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["fieldkit_home"] == "/tmp/data"


def test_write_pipeline_quota_overwrites_existing_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "pipeline:\n  quota:\n    target: 1000000\n    period: 2025-H1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    fieldkit.config.write_pipeline_quota(7500000, "2026-H2")
    result = fieldkit.config.get_pipeline_quota()
    assert result == {"target": 7500000, "period": "2026-H2"}


# ── TestLoadRawConfig (flattened) ───────────────────────────────────────────


def test_get_config_path_returns_none_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "no-config.yaml"
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", missing)
    result = fieldkit.config._loader._load_raw_config()
    assert result is None


def test_get_config_path_returns_dict_when_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: Alice\nfieldkit_home: /tmp/data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config._loader._load_raw_config()
    assert result == {"name": "Alice", "fieldkit_home": "/tmp/data"}


def test_get_config_path_returns_none_on_malformed_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("key: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config._loader._load_raw_config()
    assert result is None


def test_get_config_path_returns_none_on_non_dict_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- item1\n- item2\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config._loader._load_raw_config()
    assert result is None


# ── TestGetUserName (flattened) ─────────────────────────────────────────────


def test_get_user_name_returns_name_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: Alice Smith\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    assert fieldkit.config.get_user_name() == "Alice Smith"


def test_get_user_name_returns_empty_string_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", tmp_path / "no-config.yaml")
    assert fieldkit.config.get_user_name() == ""


def test_get_user_name_returns_empty_string_when_name_key_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp/data\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    assert fieldkit.config.get_user_name() == ""


def test_get_user_name_strips_whitespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: '  Alice  '\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    assert fieldkit.config.get_user_name() == "Alice"


# ── TestGetWatchersDir (flattened) ──────────────────────────────────────────


def test_get_watchers_dir_returns_watchers_under_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path / 'data'}\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config.get_watchers_dir()
    assert result == tmp_path / "data" / "watchers"


def test_get_watchers_dir_is_a_path_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)
    result = fieldkit.config.get_watchers_dir()
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# 4B.1 get_email_domain
# ---------------------------------------------------------------------------


# ── TestGetEmailDomain (flattened) ──────────────────────────────────────────


def test_get_email_domain_returns_configured_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the email_domain key from config.yaml."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp\nemail_domain: example.com\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)

    result = get_email_domain()

    assert result == "example.com"
    assert isinstance(result, str)


def test_get_email_domain_returns_none_when_key_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when email_domain key is not in config."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_home: /tmp\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)

    result = get_email_domain()

    assert result is None


def test_get_email_domain_returns_none_when_config_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when config file does not exist."""
    cfg = tmp_path / "no-config.yaml"
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", cfg)

    result = get_email_domain()

    assert result is None


# ---------------------------------------------------------------------------
# implementation change: _config_cache registry
# ---------------------------------------------------------------------------


# ── TestConfigCacheRegistry (flattened) ─────────────────────────────────────

_GET_CONFIG_PATH__EXPECTED_COUNT = 7

_GET_CONFIG_PATH__EXPECTED_NAMES: ClassVar[set[str]] = {
    "get_fieldkit_home",
    "_get_fieldkit_data_from_config",
    "_load_accounts_yaml",
    "_load_raw_config",
    "get_watchers_dir",
    "get_fieldkit_root",
    "get_google_token_path",
}


def test_get_config_path_cached_registry_count() -> None:
    """_CACHED must contain exactly _EXPECTED_COUNT functions.

    Fails if a new config accessor uses bare @cache instead of @_config_cache
    (would escape clear_config_caches()) or if one is accidentally removed.
    """
    assert len(_CACHED) == _GET_CONFIG_PATH__EXPECTED_COUNT, (
        f"Expected {_GET_CONFIG_PATH__EXPECTED_COUNT} functions in _CACHED, got {len(_CACHED)}. "
        f"Functions present: {[fn.__name__ for fn in _CACHED]}. "
        "If you added a new @_config_cache function, increment _EXPECTED_COUNT "
        "and add its name to _EXPECTED_NAMES. "
        "If you used bare @cache instead, switch to @_config_cache."
    )


def test_get_config_path_cached_registry_names() -> None:
    """_CACHED must contain exactly the expected function names."""
    actual_names = {fn.__name__ for fn in _CACHED}
    assert actual_names == _GET_CONFIG_PATH__EXPECTED_NAMES, (
        f"_CACHED names mismatch.\n  Expected: {sorted(_GET_CONFIG_PATH__EXPECTED_NAMES)}\n  Got:      {sorted(actual_names)}"
    )


def test_get_config_path_clear_config_caches_clears_all_registry_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_config_caches() must clear every function in _CACHED.

    Warm each cached function via get_fieldkit_data() (which triggers
    _load_raw_config and get_fieldkit_home), then assert cache_info().hits
    is zero after clear_config_caches(). Proves the registry-based
    iteration actually calls cache_clear() on all decorated functions.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")

    with patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        clear_config_caches()
        # Call twice — first call fills the cache (currsize=1, hits=0),
        # second call is a cache hit (hits=1). cache_info().hits only
        # increments on repeat calls, not on the initial fill.
        get_fieldkit_data()
        get_fieldkit_data()
        hits_before = sum(fn.cache_info().hits for fn in _CACHED)  # type: ignore[attr-defined]
        assert hits_before > 0, (
            "cache not warmed — test precondition failed: "
            f"hits={hits_before}, sizes={[fn.cache_info().currsize for fn in _CACHED]}"  # type: ignore[attr-defined]
        )

        # Clear and verify all hit counts reset to zero.
        clear_config_caches()
        hits_after = sum(fn.cache_info().hits for fn in _CACHED)  # type: ignore[attr-defined]
        assert hits_after == 0, (
            f"clear_config_caches() did not reset all caches — {hits_after} hits remain after clearing"
        )
