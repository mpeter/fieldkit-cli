"""Behavioral contracts for the canonical Salesforce sync domain."""

import json
from pathlib import Path

import pytest

import fieldkit.sf.sync as sync
from fieldkit.errors import FieldkitError, SalesforceSyncPartialError
from tests.test_sf_sync_domain import OPPORTUNITY_ID, pursuit

pytestmark = pytest.mark.unit


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "runtime" / "data"
    monkeypatch.setattr(sync, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(sync, "get_fieldkit_data", lambda: data)
    monkeypatch.setattr(sync, "get_accounts_config", lambda **kwargs: {"accounts": {"acme-corp": {}}})
    return workspace, data


@pytest.mark.parametrize("dry_run", [False, True])
def test_opportunity_publishes_one_document_and_runtime_cache(environment: tuple[Path, Path], dry_run: bool) -> None:
    workspace, data = environment
    path = pursuit(workspace, identity=OPPORTUNITY_ID)
    original = path.read_bytes()
    result = sync.sync_opportunity(
        OPPORTUNITY_ID, path, {"opportunity_id": OPPORTUNITY_ID, "stage": "Propose"}, dry_run=dry_run
    )
    assert result.frontmatter.written is not dry_run
    assert result.cache == data / "salesforce" / f"{OPPORTUNITY_ID}.json"
    if dry_run:
        assert path.read_bytes() == original
        assert not data.exists()
    else:
        assert "| Stage | Propose" in path.read_text(encoding="utf-8")
        payload = json.loads(result.cache.read_text(encoding="utf-8"))
        assert payload["stage"] == "Propose"
        assert payload["pulled_at"]
    assert not (workspace / ".cache").exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("problem", ["payload", "target", "record-kind", "missing", "outside", "invalid"])
def test_refused_opportunity_never_changes_document_or_cache(
    environment: tuple[Path, Path], problem: str, dry_run: bool
) -> None:
    workspace, data = environment
    path = pursuit(workspace, identity=OPPORTUNITY_ID if problem != "target" else "006000000000AAB")
    original = path.read_bytes()
    cache = data / "salesforce" / f"{OPPORTUNITY_ID}.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"previous":true}', encoding="utf-8")
    payload: dict[str, object] = {"opportunity_id": OPPORTUNITY_ID, "stage": "Propose"}
    if problem == "payload":
        payload["opportunity_id"] = "006000000000AAB"
    if problem == "record-kind":
        payload = {"account_id": "001000000000AAA", "industry": "Technology"}
    target = path
    if problem == "missing":
        target = path.with_name("missing.md")
    if problem == "outside":
        target = workspace.parent / "outside.md"
        target.write_bytes(original)
    identifier = "001000000000AAA" if problem == "invalid" else OPPORTUNITY_ID
    with pytest.raises(FieldkitError, match=r"identity|approved"):
        sync.sync_opportunity(identifier, target, payload, dry_run=dry_run)
    assert path.read_bytes() == original
    assert cache.read_text(encoding="utf-8") == '{"previous":true}'


@pytest.mark.parametrize("dry_run", [False, True])
def test_cache_symlink_is_refused_before_document_update(environment: tuple[Path, Path], dry_run: bool) -> None:
    workspace, data = environment
    path = pursuit(workspace, identity=OPPORTUNITY_ID)
    original = path.read_bytes()
    data.mkdir(parents=True)
    outside = workspace.parent / "outside"
    outside.mkdir()
    (data / "salesforce").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FieldkitError, match="cache destination"):
        sync.sync_opportunity(
            OPPORTUNITY_ID, path, {"opportunity_id": OPPORTUNITY_ID, "stage": "Propose"}, dry_run=dry_run
        )
    assert path.read_bytes() == original
    assert list(outside.iterdir()) == []


def test_cache_failure_is_explicit_partial_after_guarded_document_update(
    environment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _data = environment
    path = pursuit(workspace, identity=OPPORTUNITY_ID)

    def fail(path: Path, content: str) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(sync, "atomic_text_write", fail)
    with pytest.raises(SalesforceSyncPartialError, match="cache publication failed"):
        sync.sync_opportunity(OPPORTUNITY_ID, path, {"opportunity_id": OPPORTUNITY_ID, "stage": "Propose"})
    assert "| Stage | Propose" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("dry_run", [False, True])
def test_account_uses_configured_workspace_and_cache(environment: tuple[Path, Path], dry_run: bool) -> None:
    workspace, data = environment
    path = workspace / "accounts" / "acme-corp" / "account.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Acme Corp\n---\n\n# Account\n", encoding="utf-8")
    original = path.read_bytes()
    result = sync.sync_account(
        "acme-corp", {"account_id": "001000000000AAA", "industry": "Technology"}, dry_run=dry_run
    )
    assert result.frontmatter.record_kind == "account"
    assert result.cache == data / "salesforce" / "accounts" / "acme-corp.json"
    if dry_run:
        assert path.read_bytes() == original
        assert not data.exists()
    else:
        assert "sf_industry: Technology" in path.read_text(encoding="utf-8")
        assert result.cache.is_file()


def test_configured_accounts_has_no_fixture_or_directory_fallback(
    environment: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _data = environment
    pursuit(workspace)
    monkeypatch.setattr(sync, "get_accounts_config", lambda **kwargs: {})
    assert sync.configured_accounts() == ()
    with pytest.raises(FieldkitError, match="not configured"):
        sync.sync_account("acme-corp", {"industry": "Technology"})


def test_accounts_use_fresh_strict_config(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bool] = []

    def config(*, strict: bool) -> dict[str, object]:
        observed.append(strict)
        return {"accounts": {"acme-corp": {}}}

    monkeypatch.setattr(sync, "get_accounts_config", config)
    assert sync.configured_accounts() == ("acme-corp",)
    assert observed == [True]
