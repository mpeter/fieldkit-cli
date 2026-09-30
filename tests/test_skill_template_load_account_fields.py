"""Account template fields come from one validated workspace snapshot."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fieldkit.config import ConfigError
from fieldkit.skill import template
from fieldkit.skill.template import _MAX_ACCOUNT_INDEX, _load_account_fields

pytestmark = pytest.mark.unit


def _configure(root: Path, slugs: list[str], domains: list[str]) -> None:
    config = root / "config" / "accounts.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump({"accounts": {slug: {} for slug in slugs}, "internal_domains": domains}, sort_keys=False),
        encoding="utf-8",
    )


def test_account_configuration_error_propagates_without_empty_context(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.get_accounts_config", side_effect=ConfigError("Invalid accounts.yaml")) as reader,
        pytest.raises(ConfigError, match=r"accounts\.yaml"),
    ):
        _load_account_fields(tmp_path)
    reader.assert_called_once_with(strict=True, workspace_root=tmp_path)


def test_multi_account_indices_and_domains_use_one_selected_snapshot(tmp_path: Path) -> None:
    from fieldkit.config import get_accounts_config

    _configure(tmp_path, ["acme", "globex"], ["example.com"])
    with patch("fieldkit.config.get_accounts_config", wraps=get_accounts_config) as reader:
        ctx = _load_account_fields(tmp_path)
    assert ctx["accounts.all"] == "acme, globex"
    assert ctx["accounts.0"] == "acme"
    assert ctx["accounts.1"] == "globex"
    assert ctx["primary_account"] == "acme"
    assert ctx["internal_domain"] == "example.com"
    for index in range(2, _MAX_ACCOUNT_INDEX):
        assert ctx[f"accounts.{index}"] == "acme"
    reader.assert_called_once_with(strict=True, workspace_root=tmp_path)


@pytest.mark.parametrize("state", ["absent", "empty", "no_workspace"])
def test_empty_accounts_have_empty_context_defaults(tmp_path: Path, state: str) -> None:
    if state == "empty":
        _configure(tmp_path, [], [])
    ctx = _load_account_fields(None if state == "no_workspace" else tmp_path)
    assert ctx["primary_account"] == ""
    assert ctx["accounts.all"] == ""
    assert ctx["primary_pursuit"] == ""
    for index in range(_MAX_ACCOUNT_INDEX):
        assert ctx[f"accounts.{index}"] == ""


@pytest.mark.parametrize("state", ["match", "absent", "empty"])
def test_primary_pursuit_comes_from_selected_account_directory(tmp_path: Path, state: str) -> None:
    _configure(tmp_path, ["acme"], [])
    if state != "absent":
        pursuits = tmp_path / "accounts" / "acme" / "pursuits"
        pursuits.mkdir(parents=True)
        if state == "match":
            for name in ("zzz-later.md", "aaa-first.md"):
                (pursuits / name).write_text("# Fictional pursuit\n", encoding="utf-8")
    ctx = _load_account_fields(tmp_path)
    assert ctx["primary_pursuit"] == ("aaa-first" if state == "match" else "")
    assert ctx["primary_account"] == "acme"
    assert ctx["example_sf_id"] == "006Pe000000ExampleId"


@pytest.mark.parametrize("redirect", ["accounts", "account", "pursuits", "leaf"])
def test_primary_pursuit_rejects_child_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    workspace = tmp_path / "workspace"
    _configure(workspace, ["acme"], [])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside-pursuit.md").write_text("# Fictional outside pursuit\n", encoding="utf-8")
    if redirect == "accounts":
        (outside / "acme/pursuits").mkdir(parents=True)
        (outside / "acme/pursuits/outside-pursuit.md").write_text("# Fictional pursuit\n", encoding="utf-8")
        (workspace / "accounts").symlink_to(outside, target_is_directory=True)
    elif redirect == "account":
        (workspace / "accounts").mkdir()
        (outside / "pursuits").mkdir()
        (outside / "pursuits/outside-pursuit.md").write_text("# Fictional pursuit\n", encoding="utf-8")
        (workspace / "accounts/acme").symlink_to(outside, target_is_directory=True)
    elif redirect == "pursuits":
        (workspace / "accounts/acme").mkdir(parents=True)
        (workspace / "accounts/acme/pursuits").symlink_to(outside, target_is_directory=True)
    else:
        (workspace / "accounts/acme/pursuits").mkdir(parents=True)
        (workspace / "accounts/acme/pursuits/selected.md").symlink_to(outside / "outside-pursuit.md")
    outside_dirs = [outside, *(path for path in outside.rglob("*") if path.is_dir())]
    outside_ids = {(path.stat().st_dev, path.stat().st_ino) for path in outside_dirs}
    outside_scans: list[int] = []
    original = os.scandir

    @contextmanager
    def observed_scan(path: Path | int) -> Iterator[Iterator[os.DirEntry[str]]]:
        observed = os.fstat(path) if isinstance(path, int) else path.stat()
        if (observed.st_dev, observed.st_ino) in outside_ids:
            outside_scans.append(observed.st_ino)
        with original(path) as entries:
            yield entries

    monkeypatch.setattr(os, "scandir", observed_scan)
    with pytest.raises(ConfigError, match="primary pursuit inventory") as caught:
        _load_account_fields(workspace)
    assert str(tmp_path) not in str(caught.value)
    assert outside_scans == []


@pytest.mark.parametrize("slug", ["..", "../../outside", "bad/slug", "bad\\slug", "bad:slug", "bad\nslug", ""])
def test_primary_pursuit_rejects_unsafe_primary_account_slug(tmp_path: Path, slug: str) -> None:
    _configure(tmp_path, [slug], [])
    with pytest.raises(ConfigError, match="primary pursuit inventory"):
        _load_account_fields(tmp_path)


def test_primary_pursuit_rejects_markdown_directory(tmp_path: Path) -> None:
    _configure(tmp_path, ["acme"], [])
    (tmp_path / "accounts/acme/pursuits/not-a-pursuit.md").mkdir(parents=True)
    with pytest.raises(ConfigError, match="primary pursuit inventory"):
        _load_account_fields(tmp_path)


def test_primary_pursuit_inventory_counts_non_markdown_entries_toward_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, ["acme"], [])
    pursuits = tmp_path / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    for name in ("aaa.md", "zzz.md", "unrelated.txt", "fourth.txt"):
        (pursuits / name).write_text("Fictional content\n", encoding="utf-8")
    monkeypatch.setattr(template, "_MAX_PRIMARY_PURSUIT_ENTRIES", 2)
    observed: list[str] = []
    original = os.scandir

    @contextmanager
    def observed_scan(path: Path | int) -> Iterator[Iterator[os.DirEntry[str]]]:
        with original(path) as entries:

            def counted_entries() -> Iterator[os.DirEntry[str]]:
                for entry in entries:
                    observed.append(entry.name)
                    yield entry

            yield counted_entries()

    monkeypatch.setattr(os, "scandir", observed_scan)
    with pytest.raises(ConfigError, match="primary pursuit inventory"):
        _load_account_fields(tmp_path)
    assert len(observed) == 3


def test_primary_pursuit_inventory_accepts_exact_entry_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(tmp_path, ["acme"], [])
    pursuits = tmp_path / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "selected.md").write_text("# Fictional pursuit\n", encoding="utf-8")
    (pursuits / "other.txt").write_text("Fictional content\n", encoding="utf-8")
    monkeypatch.setattr(template, "_MAX_PRIMARY_PURSUIT_ENTRIES", 2)
    ctx = _load_account_fields(tmp_path)
    assert ctx["primary_pursuit"] == "selected"


def test_primary_pursuit_inventory_iteration_failure_is_not_empty_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, ["acme"], [])
    pursuits = tmp_path / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "aaa.md").write_text("# Fictional pursuit\n", encoding="utf-8")
    original = os.scandir

    @contextmanager
    def failed_scan(path: Path | int) -> Iterator[Iterator[os.DirEntry[str]]]:
        with original(path) as entries:

            def broken_entries() -> Iterator[os.DirEntry[str]]:
                yield next(entries)
                raise OSError("private-sentinel")

            yield broken_entries()

    monkeypatch.setattr(os, "scandir", failed_scan)
    with pytest.raises(ConfigError, match="primary pursuit inventory") as caught:
        _load_account_fields(tmp_path)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses ordinary directory permissions")
def test_primary_pursuit_inventory_permission_failure_is_not_empty_context(tmp_path: Path) -> None:
    _configure(tmp_path, ["acme"], [])
    pursuits = tmp_path / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    pursuits.chmod(0)
    try:
        with pytest.raises(ConfigError, match="primary pursuit inventory"):
            _load_account_fields(tmp_path)
    finally:
        pursuits.chmod(0o700)


def test_primary_pursuit_inventory_accepts_workspace_root_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _configure(workspace, ["acme"], [])
    pursuits = workspace / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "chosen.md").write_text("# Fictional pursuit\n", encoding="utf-8")
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    ctx = _load_account_fields(alias)
    assert ctx["primary_pursuit"] == "chosen"
