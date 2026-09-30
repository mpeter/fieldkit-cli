"""Account routing never turns invalid selected configuration into no match."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError, get_accounts_config
from fieldkit.ingest.router import route_by_domains

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "contents",
    [b"accounts: [\n", b"accounts: {}\naccounts: {}\n", b"accounts: [acme]\n", b"\xff"],
)
def test_routing_rejects_invalid_selected_configuration(tmp_path: Path, contents: bytes) -> None:
    config = tmp_path / "config" / "accounts.yaml"
    config.parent.mkdir()
    config.write_bytes(contents)
    with pytest.raises(ConfigError, match=r"accounts\.yaml") as caught:
        route_by_domains(["acme-corp.example.com"], data_root=tmp_path)
    assert str(tmp_path) not in str(caught.value)
    assert config.read_bytes() == contents


def test_routing_reads_selected_configuration_fresh(tmp_path: Path) -> None:
    config = tmp_path / "config" / "accounts.yaml"
    config.parent.mkdir()
    config.write_text("accounts: {acme: {domains: [acme-corp.example.com]}}\n", encoding="utf-8")
    first = route_by_domains(["acme-corp.example.com"], data_root=tmp_path)
    assert first.accounts == ["acme"]
    config.write_text("accounts: {example: {domains: [acme-corp.example.com]}}\n", encoding="utf-8")
    second = route_by_domains(["acme-corp.example.com"], data_root=tmp_path)
    assert second.accounts == ["example"]


def test_internal_and_external_domains_share_one_configuration_read(tmp_path: Path) -> None:
    config = tmp_path / "config" / "accounts.yaml"
    config.parent.mkdir()
    config.write_text(
        "internal_domains: [example.com]\naccounts: {acme: {domains: [acme-corp.example.com]}}\n", encoding="utf-8"
    )
    with patch("fieldkit.ingest.router.get_accounts_config", wraps=get_accounts_config) as read:
        result = route_by_domains(["example.com", "acme-corp.example.com"], data_root=tmp_path)
    assert result.accounts == ["acme"]
    assert result.is_internal is False
    read.assert_called_once_with(strict=True, workspace_root=tmp_path)
