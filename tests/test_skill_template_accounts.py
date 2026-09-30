"""Skill territory context uses validated account configuration."""

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from fieldkit.config import ConfigError, get_accounts_config
from fieldkit.skill.template import (
    _load_account_fields,
    _load_config_fields,
    _territory_from_accounts,
    build_template_ctx,
)

pytestmark = pytest.mark.unit


def _context_workspace(root: Path, account: str, territory: str) -> None:
    configuration = root / "config"
    configuration.mkdir(parents=True)
    (configuration / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": {account: {"sf_territory": territory}}}), encoding="utf-8"
    )
    (configuration / "identity.yaml").write_text("salesforce_user_id: 005Example\n", encoding="utf-8")


def test_context_observes_personal_configuration_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.config import _loader

    selected = tmp_path / "selected"
    other = tmp_path / "other"
    _context_workspace(selected, "acme", "SELECTED")
    _context_workspace(other, "globex", "OTHER")
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"name": "Original", "fieldkit_home": str(selected)}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    original = _loader._load_raw_config_uncached
    reads = 0

    def changing_read(*, strict: bool) -> dict[str, object] | None:
        nonlocal reads
        reads += 1
        observed = original(strict=strict)
        personal.write_text(yaml.safe_dump({"name": "Changed", "fieldkit_home": str(other)}), encoding="utf-8")
        return observed

    monkeypatch.setattr(_loader, "_load_raw_config_uncached", changing_read)
    result = build_template_ctx()
    assert result["name"] == "Original"
    assert result["fieldkit_home"] == str(selected)
    assert result["primary_account"] == "acme"
    assert result["territory"] == "SELECTED"
    assert reads == 1


def test_context_observes_selected_accounts_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fieldkit.config as config
    from fieldkit.config import _loader

    _context_workspace(tmp_path / "selected", "acme", "SELECTED")
    selected = tmp_path / "selected"
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": str(selected)}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    original = config.get_accounts_config
    reads = 0

    def changing_read(*, strict: bool = False, workspace_root: Path | None = None) -> dict[str, object]:
        nonlocal reads
        reads += 1
        observed = original(strict=strict, workspace_root=workspace_root)
        (selected / "config/accounts.yaml").write_text(
            "accounts:\n  globex: {sf_territory: CHANGED}\n", encoding="utf-8"
        )
        return observed

    monkeypatch.setattr(config, "get_accounts_config", changing_read)
    result = build_template_ctx()
    assert result["primary_account"] == "acme"
    assert result["territory"] == "SELECTED"
    assert reads == 1


@pytest.mark.parametrize("field", ["name", "email", "role", "company", "fieldkit_home"])
@pytest.mark.parametrize("value", [["private-sentinel"], {"private-sentinel": "value"}, True])
def test_context_rejects_non_string_personal_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    from fieldkit.config import _loader

    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({field: value}), encoding="utf-8")
    original = personal.read_bytes()
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    with pytest.raises(ConfigError) as caught:
        build_template_ctx()
    assert "private-sentinel" not in str(caught.value)
    assert personal.read_bytes() == original


@pytest.mark.parametrize("field", ["territory", "salesforce_user_id"])
@pytest.mark.parametrize("value", [["private-sentinel"], {"private-sentinel": "value"}, True])
def test_context_rejects_non_string_identity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    from fieldkit.config import _loader

    _context_workspace(tmp_path / "selected", "acme", "SELECTED")
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": str(tmp_path / "selected")}), encoding="utf-8")
    identity = tmp_path / "selected/config/identity.yaml"
    identity.write_text(yaml.safe_dump({"identity": {field: value}}), encoding="utf-8")
    original = identity.read_bytes()
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    with pytest.raises(ConfigError) as caught:
        build_template_ctx()
    assert "private-sentinel" not in str(caught.value)
    assert identity.read_bytes() == original


@pytest.mark.parametrize("root", ["relative-workspace", "./workspace", "   "])
def test_context_rejects_non_absolute_workspace_before_account_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    import fieldkit.config as config
    from fieldkit.config import _loader

    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": root}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    reads: list[bool] = []

    def unexpected_read(*, strict: bool = False, workspace_root: Path | None = None) -> dict[str, object]:
        reads.append(strict)
        return {}

    monkeypatch.setattr(config, "get_accounts_config", unexpected_read)
    with pytest.raises(ConfigError):
        build_template_ctx()
    assert reads == []


def test_absent_identity_keeps_empty_territory_despite_account_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.config import _loader

    selected = tmp_path / "selected"
    _context_workspace(selected, "acme", "SELECTED")
    (selected / "config/identity.yaml").unlink()
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": str(selected)}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    result = build_template_ctx()
    assert result["territory"] == ""
    assert result["salesforce_user_id"] == ""
    assert result["primary_account"] == "acme"


def test_context_canonicalizes_workspace_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.config import _loader

    selected = tmp_path / "selected"
    _context_workspace(selected, "acme", "SELECTED")
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(selected, target_is_directory=True)
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": str(alias)}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    result = build_template_ctx()
    assert result["fieldkit_home"] == str(selected)
    assert result["primary_account"] == "acme"
    assert result["territory"] == "SELECTED"


@pytest.mark.parametrize("value", [["private-sentinel"], {"private-sentinel": "value"}, True])
def test_context_rejects_non_string_account_territory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    from fieldkit.config import _loader

    selected = tmp_path / "selected"
    _context_workspace(selected, "acme", "SELECTED")
    accounts = selected / "config/accounts.yaml"
    accounts.write_text(yaml.safe_dump({"accounts": {"acme": {"sf_territory": value}}}), encoding="utf-8")
    personal = tmp_path / "config.yaml"
    personal.write_text(yaml.safe_dump({"fieldkit_home": str(selected)}), encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    with pytest.raises(ConfigError, match="sf_territory") as caught:
        build_template_ctx()
    assert "private-sentinel" not in str(caught.value)


def test_absent_optional_template_scalars_remain_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.config import _loader

    personal = tmp_path / "config.yaml"
    personal.write_text("name: null\nrole: null\ncompany: null\nemail: null\nfieldkit_home: null\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", personal)
    result = build_template_ctx()
    assert result["name"] == ""
    assert all(result[field] == "" for field in ("role", "company", "email", "fieldkit_home", "territory"))


@pytest.mark.parametrize("content", [b"accounts: [\n", b"accounts: {}\naccounts: {}\n", b"\xff"])
def test_invalid_territory_configuration_is_not_empty_context(tmp_path: Path, content: bytes) -> None:
    config = tmp_path / "config" / "accounts.yaml"
    config.parent.mkdir()
    config.write_bytes(content)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        _territory_from_accounts(get_accounts_config(strict=True, workspace_root=tmp_path))
    assert config.read_bytes() == content


def test_territory_uses_first_external_configured_account(tmp_path: Path) -> None:
    config = tmp_path / "config" / "accounts.yaml"
    config.parent.mkdir()
    config.write_text(
        "accounts:\n  staff: {internal: true, sf_territory: INTERNAL}\n"
        "  acme: {sf_territory: SELECTED}\n  example: {sf_territory: OTHER}\n",
        encoding="utf-8",
    )
    result = _territory_from_accounts(get_accounts_config(strict=True, workspace_root=tmp_path))
    assert result == "SELECTED"


def test_absent_accounts_configuration_has_no_territory(tmp_path: Path) -> None:
    result = _territory_from_accounts(get_accounts_config(strict=True, workspace_root=tmp_path))
    assert result == ""


@pytest.mark.parametrize("reader", [_load_config_fields, build_template_ctx])
@pytest.mark.parametrize("content", [b"name: [\n", b"name: Jane\nname: Other\n", b"\xff"])
def test_invalid_personal_configuration_is_not_empty_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: Callable[[], dict[str, str]], content: bytes
) -> None:
    from fieldkit.config import _loader

    path = tmp_path / "config.yaml"
    path.write_bytes(content)
    monkeypatch.setattr(_loader, "CONFIG_PATH", path)
    with pytest.raises(ConfigError):
        reader()
    assert path.read_bytes() == content


@pytest.mark.parametrize("content", [b"territory: [\n", b"territory: ONE\nterritory: TWO\n", b"\xff"])
def test_invalid_identity_configuration_is_not_empty_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    from fieldkit.config import _loader

    path = tmp_path / "config.yaml"
    path.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", path)
    identity = tmp_path / "config" / "identity.yaml"
    identity.parent.mkdir()
    identity.write_bytes(content)
    with pytest.raises(ConfigError):
        build_template_ctx()
    assert identity.read_bytes() == content


@pytest.mark.parametrize("redirect", ["directory", "leaf"])
def test_identity_child_redirect_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str) -> None:
    from fieldkit.config import _loader

    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(f"fieldkit_home: {root}\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", config)
    outside = tmp_path / "outside"
    outside.mkdir()
    identity = outside / "identity.yaml"
    identity.write_text("territory: OUTSIDE\n", encoding="utf-8")
    if redirect == "directory":
        (root / "config").symlink_to(outside, target_is_directory=True)
    else:
        (root / "config").mkdir()
        (root / "config" / "identity.yaml").symlink_to(identity)
    with pytest.raises(ConfigError):
        build_template_ctx()
    assert identity.read_text(encoding="utf-8") == "territory: OUTSIDE\n"


@pytest.mark.parametrize("content", [b"accounts: [\n", b"accounts: {}\naccounts: {}\n", b"\xff"])
def test_invalid_accounts_configuration_cannot_render_empty_account_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    from fieldkit.config import _loader

    config = tmp_path / "config.yaml"
    config.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", config)
    account_path = tmp_path / "config" / "accounts.yaml"
    account_path.parent.mkdir()
    account_path.write_bytes(content)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        _load_account_fields(tmp_path)
    assert account_path.read_bytes() == content
