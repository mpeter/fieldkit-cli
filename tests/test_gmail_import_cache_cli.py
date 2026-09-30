"""CLI contract for explicit legacy Gmail cache import."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.import_cache import cli
from fieldkit.errors import SQLiteSnapshotError
from fieldkit.gmail.cache_import import GmailCacheImportPreview, GmailCacheImportResult

pytestmark = pytest.mark.unit


def test_import_cache_json_reports_only_managed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "managed.db"
    source.write_bytes(b"fixture")
    observed: list[tuple[Path, Path]] = []

    def import_cache(source_path: Path, target_path: Path) -> GmailCacheImportResult:
        observed.append((source_path, target_path))
        return GmailCacheImportResult(generation=4, table_counts={"messages": 2, "threads": 1})

    monkeypatch.setattr("fieldkit.commands.gmail.import_cache.import_gmail_cache", import_cache)

    result = CliRunner().invoke(
        cli,
        ["--source", str(source), "--db", str(target), "--json"],
    )

    assert result.exit_code == 0
    assert observed == [(source, target)]
    assert json.loads(result.output) == {
        "generation": 4,
        "status": "ok",
        "table_counts": {"messages": 2, "threads": 1},
    }
    assert str(source) not in result.output
    assert str(target) not in result.output


def test_import_cache_preserves_typed_data_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "managed.db"
    source.write_bytes(b"fixture")

    def fail_import(source_path: Path, target_path: Path) -> GmailCacheImportResult:
        del source_path, target_path
        raise SQLiteSnapshotError("Legacy Gmail cache schema is not supported", reason="unverified")

    monkeypatch.setattr("fieldkit.commands.gmail.import_cache.import_gmail_cache", fail_import)

    result = CliRunner().invoke(cli, ["--source", str(source), "--db", str(target)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SQLiteSnapshotError)
    assert str(source) not in result.output
    assert str(target) not in result.output


def test_import_cache_dry_run_uses_the_write_free_preview_and_payload_free_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "managed.db"
    source.write_bytes(b"fixture")
    observed: list[tuple[Path, Path]] = []

    def preview(source_path: Path, target_path: Path) -> GmailCacheImportPreview:
        observed.append((source_path, target_path))
        return GmailCacheImportPreview(table_counts={"messages": 2, "threads": 1})

    monkeypatch.setattr("fieldkit.commands.gmail.import_cache.preview_gmail_cache_import", preview)
    monkeypatch.setattr(
        "fieldkit.commands.gmail.import_cache.import_gmail_cache",
        lambda *_args: pytest.fail("dry-run must not import"),
    )

    result = CliRunner().invoke(
        cli,
        ["--source", str(source), "--db", str(target), "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert observed == [(source, target)]
    assert json.loads(result.output) == {
        "dry_run": True,
        "status": "preview",
        "table_counts": {"messages": 2, "threads": 1},
    }
    assert str(source) not in result.output
    assert str(target) not in result.output
