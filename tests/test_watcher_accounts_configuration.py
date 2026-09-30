"""Watchers cannot disguise invalid account settings as optional absence."""

import importlib
from contextlib import nullcontext
from pathlib import Path

import pytest

from fieldkit.config import ConfigError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("module_name", ["backstory_health", "slack_threads"])
@pytest.mark.parametrize("content", [b"accounts: [\n", b"accounts: {}\naccounts: {}\n", b"\xff"])
def test_watcher_account_read_preserves_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_name: str, content: bytes
) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_bytes(content)
    monkeypatch.setattr("fieldkit.config._accounts.get_config_path", lambda name: config)
    module = importlib.import_module(f"fieldkit.watch.{module_name}")
    monkeypatch.setattr(module, "watcher_logging", lambda *_args, **_kwargs: nullcontext())
    with pytest.raises(ConfigError, match=r"accounts\.yaml") as caught:
        if module_name == "backstory_health":
            module._run_backstory_health(threshold=60, account=None, dry_run=False)
        else:
            module._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)
    assert str(tmp_path) not in str(caught.value)
    assert config.read_bytes() == content
