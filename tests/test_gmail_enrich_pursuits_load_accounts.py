import importlib
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def test_enrichment_selection_uses_fresh_strict_configuration() -> None:
    module = importlib.import_module("fieldkit.commands.gmail.enrich_pursuits")
    with patch.object(
        module,
        "get_accounts_config",
        side_effect=[{"accounts": {"acme-corp": {}}}, {"accounts": {"example-org": {}}}],
    ) as read:
        first = module._resolve_enrich_accounts(None, as_json=True)
        second = module._resolve_enrich_accounts(None, as_json=True)
    assert first == ["acme-corp"]
    assert second == ["example-org"]
    assert read.call_count == 2
    read.assert_called_with(strict=True)


def test_invalid_configuration_stops_enrichment_selection() -> None:
    from fieldkit.config._loader import ConfigError

    module = importlib.import_module("fieldkit.commands.gmail.enrich_pursuits")
    with (
        patch.object(module, "get_accounts_config", side_effect=ConfigError("Invalid accounts.yaml")),
        pytest.raises(ConfigError, match=r"Invalid accounts\.yaml"),
    ):
        module._resolve_enrich_accounts(None, as_json=True)
