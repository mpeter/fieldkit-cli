"""All configuration entry points reject ambiguity without reflecting inputs."""

from pathlib import Path

import pytest

from fieldkit.config import ConfigError, _accounts, _loader
from fieldkit.config._paths import get_fieldkit_home

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "payload",
    [b"accounts:\n  acme-corp: {}\n  acme-corp: {}\n", b"accounts: [\n", b"\xff"],
    ids=["duplicate", "malformed", "undecodable"],
)
def test_accounts_setter_rejects_invalid_existing_file_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_bytes(payload)
    monkeypatch.setattr(_accounts, "get_config_path", lambda _name: path)

    with pytest.raises(ConfigError, match="Existing") as caught:
        _accounts.set_sf_territory_id_for_account("acme-corp", "0MI000000000009")

    assert str(tmp_path) not in str(caught.value)
    assert path.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("present", [False, True])
def test_strict_accounts_reader_distinguishes_absence_from_valid_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    path = tmp_path / "accounts.yaml"
    expected: dict[str, object] = {"accounts": {"acme-corp": {}}} if present else {}
    if present:
        path.write_text("accounts:\n  acme-corp: {}\n", encoding="utf-8")
    monkeypatch.setattr(_accounts, "get_config_path", lambda _name: path)

    result = _accounts.get_accounts_config(strict=True)

    assert result == expected


@pytest.mark.parametrize(
    "payload",
    [
        "accounts:\n  acme-corp: {}\n  acme-corp: {}\n",
        "accounts:\n  acme-corp:\n    domains: []\n    domains: [example.com]\n",
        "accounts: []\n",
        "accounts:\n  acme-corp: []\n",
        "internal_domains: example.com\n",
    ],
    ids=["duplicate-account", "duplicate-nested", "accounts-list", "account-list", "domains-scalar"],
)
def test_accounts_reader_rejects_ambiguous_or_invalid_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(_accounts, "get_config_path", lambda _name: path)
    _loader.clear_config_caches()

    with pytest.raises(ConfigError, match=r"accounts\.yaml") as caught:
        _accounts.get_accounts_config(strict=True)

    assert str(tmp_path) not in str(caught.value)
    assert "acme-corp" not in str(caught.value)
    result = _accounts.get_accounts_config()
    assert result == {}


@pytest.mark.parametrize("kind", ["missing", "relative", "loop"])
def test_workspace_accessor_diagnostics_do_not_reflect_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "config.yaml"
    if kind == "relative":
        path.write_text("fieldkit_home: fictional-private-workspace\n", encoding="utf-8")
    elif kind == "loop":
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        path.write_text(f"fieldkit_home: {loop}\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", path)
    _loader.clear_config_caches()

    try:
        with pytest.raises(ConfigError) as caught:
            get_fieldkit_home()
        assert str(tmp_path) not in str(caught.value)
        assert "fictional-private-workspace" not in str(caught.value)
    finally:
        _loader.clear_config_caches()


@pytest.mark.parametrize("mode", ["strict", "permissive", "one-off"])
@pytest.mark.parametrize(
    "payload",
    [b"custom: first\ncustom: second\n", b"token: [fictional-sensitive-token\n", b"\xff"],
    ids=["duplicate", "malformed", "undecodable"],
)
def test_config_readers_fail_without_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, payload: bytes
) -> None:
    path = tmp_path / "private-config.yaml"
    path.write_bytes(payload)
    monkeypatch.setattr(_loader, "CONFIG_PATH", path)

    if mode == "strict":
        with pytest.raises(ConfigError) as caught:
            _loader._load_raw_config_uncached(strict=True)
        assert str(tmp_path) not in str(caught.value)
        assert "fictional-sensitive-token" not in str(caught.value)
    else:
        result = (
            _loader._read_config_dict(path) if mode == "one-off" else _loader._load_raw_config_uncached(strict=False)
        )
        assert result is None
    assert path.read_bytes() == payload


def test_config_model_errors_do_not_reflect_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("fieldkit_home: [fictional-sensitive-token]\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", path)

    with pytest.raises(ConfigError) as caught:
        _loader._load_raw_config_uncached(strict=True)

    assert "fictional-sensitive-token" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
