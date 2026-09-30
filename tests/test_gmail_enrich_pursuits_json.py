"""JSON contract tests for ``fieldkit gmail enrich-pursuits``."""

import json
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_json_reports_written_and_skipped_accounts(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts_root = tmp_path / "accounts"
    (accounts_root / "acme-corp").mkdir(parents=True)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}, "example-org": {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts_root),
        patch.object(ep, "build_account_report", side_effect=["report", None]),
    ):
        result = CliRunner().invoke(ep.cli, ["--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {
        "accounts": [
            {
                "account": "acme-corp",
                "path": str(accounts_root / "acme-corp" / "gmail-intel.md"),
                "status": "written",
            },
            {"account": "example-org", "path": None, "status": "skipped"},
        ],
        "error": None,
        "requested": None,
    }


def test_json_unknown_account_is_single_error_document() -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    with patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}}}):
        result = CliRunner().invoke(ep.cli, ["--account", "missing", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload == {"accounts": [], "error": "account_not_found", "requested": "missing"}


def test_json_failure_identifies_failed_account_after_completed_accounts(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts_root = tmp_path / "accounts"
    (accounts_root / "acme-corp").mkdir(parents=True)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}, "example-org": {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts_root),
        patch.object(ep, "build_account_report", side_effect=["report", 3]),
    ):
        result = CliRunner().invoke(ep.cli, ["--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 3
    assert payload["accounts"][-1] == {"account": "example-org", "path": None, "status": "failed"}


def test_report_write_cleans_up_temporary_when_replace_fails(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    account_root = tmp_path / "acme-corp"
    account_root.mkdir()
    target = account_root / "gmail-intel.md"
    target.write_text("previous report", encoding="utf-8")
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}}}),
        patch.object(ep, "get_accounts_root", return_value=tmp_path),
        patch.object(ep, "build_account_report", return_value="report"),
        patch.object(Path, "replace", MagicMock(side_effect=PermissionError("denied"))),
        pytest.raises(PermissionError, match="denied"),
    ):
        CliRunner().invoke(ep.cli, ["--json"], catch_exceptions=False)

    assert target.read_text(encoding="utf-8") == "previous report"
    assert list(account_root.iterdir()) == [target]


@pytest.mark.parametrize("content", ["accounts: [\n", "accounts: {acme: {}}\naccounts: {}\n", "accounts: [acme]\n"])
def test_invalid_configuration_exits_three_without_report_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.commands.gmail import enrich_pursuits as ep

    config = tmp_path / "accounts.yaml"
    config.write_text(content, encoding="utf-8")
    monkeypatch.setattr("fieldkit.config._accounts.get_config_path", lambda filename: config)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    before = config.read_bytes()
    with patch.object(ep, "build_account_report", side_effect=AssertionError("unexpected report")) as build:
        result = main(["gmail", "enrich-pursuits", "--json"])
    assert result == 3
    build.assert_not_called()
    assert config.read_bytes() == before
    assert list(tmp_path.iterdir()) == [config]
    output = capsys.readouterr()
    assert str(tmp_path) not in output.out + output.err


@pytest.mark.parametrize("redirect", ["traversal", "symlink"])
def test_redirected_account_cannot_read_or_write_outside_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts = tmp_path / "accounts"
    accounts.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    slug = "../outside" if redirect == "traversal" else "acme"
    if redirect == "symlink":
        (accounts / slug).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {slug: {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts),
        patch.object(ep, "build_account_report", return_value="report") as build,
    ):
        result = main(["gmail", "enrich-pursuits", "--json"])
    assert result == 3
    build.assert_not_called()
    assert list(outside.iterdir()) == []


def test_keyword_reader_rejects_symlinked_pursuit(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    outside = tmp_path / "outside.md"
    outside.write_text("What We're Selling: automation", encoding="utf-8")
    pursuit = tmp_path / "pursuit.md"
    pursuit.symlink_to(outside)
    with pytest.raises(ValueError, match="Cannot read stable regular text file"):
        ep.get_pursuit_keywords(str(pursuit))


@pytest.mark.parametrize("scan_error", [PermissionError, NotADirectoryError, OSError])
@pytest.mark.parametrize("scan_phase", ["open", "iterate"])
def test_pursuit_scan_failure_is_sanitized_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scan_error: type[OSError],
    scan_phase: str,
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts = tmp_path / "accounts"
    pursuits = accounts / "acme-corp" / "pursuits"
    pursuits.mkdir(parents=True)
    real_scandir = os.scandir

    def scan(path: Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if path == pursuits:
            if scan_phase == "open":
                raise scan_error(str(tmp_path))
            entries = MagicMock()
            entries.__iter__.side_effect = scan_error(str(tmp_path))
            return entries
        return real_scandir(path)

    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts),
        patch("os.scandir", side_effect=scan),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_domain.connect") as connect,
        patch.object(ep, "atomic_text_write") as writer,
    ):
        result = main(["gmail", "enrich-pursuits", "--json"])
    assert result == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] is not None
    assert str(tmp_path) not in output.out + output.err
    connect.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize("pursuit_state", ["missing", "symlink"])
def test_pursuit_enumeration_preserves_missing_skip_and_symlink_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pursuit_state: str
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts = tmp_path / "accounts"
    accounts.mkdir()
    if pursuit_state == "symlink":
        pursuits = accounts / "acme-corp" / "pursuits"
        pursuits.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("Fictional pursuit", encoding="utf-8")
        (pursuits / "planning.md").symlink_to(outside)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_domain.connect") as connect,
        patch.object(ep, "read_pursuit_text_snapshot") as read,
        patch.object(ep, "atomic_text_write") as writer,
    ):
        result = main(["gmail", "enrich-pursuits", "--json"])
    assert result == (0 if pursuit_state == "missing" else 3)
    if pursuit_state == "missing":
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] is None
        assert payload["accounts"][0]["status"] == "skipped"
    connect.assert_not_called()
    read.assert_not_called()
    writer.assert_not_called()


def test_pursuit_enumeration_limit_is_retryable_without_cache_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts = tmp_path / "accounts"
    pursuits = accounts / "acme-corp" / "pursuits"
    pursuits.mkdir(parents=True)
    for name in ("one.txt", "two.txt", "three.txt"):
        (pursuits / name).write_text("Fictional note", encoding="utf-8")
    monkeypatch.setattr(ep, "_MAX_PURSUIT_DIRECTORY_ENTRIES", 2, raising=False)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    with (
        patch.object(ep, "get_accounts_config", return_value={"accounts": {"acme-corp": {}}}),
        patch.object(ep, "get_accounts_root", return_value=accounts),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_domain.connect") as connect,
        patch.object(ep, "atomic_text_write") as writer,
    ):
        result = main(["gmail", "enrich-pursuits", "--json"])
    assert result == 1
    assert json.loads(capsys.readouterr().out)["error"] is not None
    connect.assert_not_called()
    writer.assert_not_called()
