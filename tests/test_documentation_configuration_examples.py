"""Regression checks for structured configuration examples in public docs."""

import re
from pathlib import Path

import pytest
import yaml

import fieldkit.config._accounts as accounts_config
import fieldkit.config._loader as config_loader
from fieldkit.config._accounts import _load_accounts_yaml
from fieldkit.config._loader import clear_config_caches
from fieldkit.config._quota import get_pipeline_quota
from fieldkit.config._schema import _FieldkitConfig

pytestmark = pytest.mark.unit

_CONFIG_REFERENCE = Path("docs/reference/config-file.md")


def _yaml_examples() -> list[dict[str, object]]:
    """Load YAML fenced blocks exactly as published in the configuration reference."""
    content = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", content, flags=re.DOTALL)
    return [yaml.safe_load(block) for block in blocks]


def test_configuration_reference_yaml_examples_are_accepted_by_their_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published YAML examples parse through the same configuration readers users invoke."""
    quota, shadowbot, accounts = _yaml_examples()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(quota), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()

    try:
        assert get_pipeline_quota() == {"target": 5_000_000, "period": "2026-H2"}
    finally:
        clear_config_caches()

    _FieldkitConfig.model_validate({"shadowbot": shadowbot})

    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    monkeypatch.setattr(accounts_config, "get_config_path", lambda _: accounts_path)
    clear_config_caches()

    try:
        assert _load_accounts_yaml() == accounts
    finally:
        clear_config_caches()
