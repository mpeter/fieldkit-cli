"""Local watcher process outcomes include actual status persistence failures."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from fieldkit.watch import close_date_countdown, contract_expiry, pursuit_stalls, status
from fieldkit.watch.status import WatcherRunResult, get_daily_run_snapshot
from fieldkit.watch.status import write_run_status as real_write_run_status

pytestmark = pytest.mark.unit
Watcher = Literal["pursuit-stalls", "close-date-countdown", "contract-expiry"]


@pytest.fixture
def workspace(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    today = datetime.now(UTC).date()
    path = documented_workspace / "accounts/acme-corp/pursuits/deal.md"
    path.write_text(
        f"---\nstage: qualify\nlast-transition: {today}\nsf_close_date: {today + timedelta(days=45)}\n---\nBody\n",
        encoding="utf-8",
    )
    for module in (pursuit_stalls, close_date_countdown, contract_expiry):
        monkeypatch.setattr(module, "write_run_status", real_write_run_status)
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: documented_workspace)
    monkeypatch.setattr(pursuit_stalls, "get_daily_run_snapshot", get_daily_run_snapshot)
    return documented_workspace


def _run(watcher: Watcher, *, dry_run: bool = False) -> WatcherRunResult:
    if watcher == "pursuit-stalls":
        return pursuit_stalls._run_pursuit_stalls(threshold=14, account=None, dry_run=dry_run)
    if watcher == "close-date-countdown":
        return close_date_countdown._run_countdown(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=dry_run,
            as_json=True,
        )
    return contract_expiry._run_contract_expiry(account_filter=None, dry_run=dry_run, as_json=True)


def _fail_status_replace(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = Path.replace
    failed: list[Path] = []

    def replace(path: Path, target: str | Path) -> Path:
        destination = Path(target)
        if destination.name == "watcher-run-status.json":
            failed.append(destination)
            raise OSError("fictional status storage failure")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    return failed


@pytest.mark.parametrize("watcher", ["pursuit-stalls", "close-date-countdown", "contract-expiry"])
def test_status_only_persistence_failure_is_nonpassing(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    watcher: Watcher,
) -> None:
    failed = _fail_status_replace(monkeypatch)
    result = _run(watcher)
    assert result == WatcherRunResult("fatal", True, "failed")
    assert failed == [workspace / "watchers/watcher-run-status.json"]
    assert not failed[0].exists()
    captured = capsys.readouterr()
    if watcher != "pursuit-stalls":
        report = json.loads(captured.out)
        assert report["outcome"] == "fatal"
        assert report["failures"] == 1
        assert report["records_checked"] == 1


@pytest.mark.parametrize("watcher", ["pursuit-stalls", "close-date-countdown", "contract-expiry"])
def test_dry_run_never_writes_even_when_status_destination_would_fail(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    watcher: Watcher,
) -> None:
    before = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    failed = _fail_status_replace(monkeypatch)
    result = _run(watcher, dry_run=True)
    assert result == WatcherRunResult("ok", True, "skipped")
    assert failed == []
    assert {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("watcher", ["pursuit-stalls", "close-date-countdown", "contract-expiry"])
def test_clean_scan_persists_success(workspace: Path, watcher: Watcher) -> None:
    result = _run(watcher)
    assert result == WatcherRunResult("ok", True, "written")
    report = json.loads((workspace / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))[watcher]
    assert report["outcome"] == "ok"
    assert report["records_checked"] == 1
    assert report["failures"] == 0


@pytest.mark.parametrize("watcher", ["pursuit-stalls", "close-date-countdown", "contract-expiry"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_record_failures_remain_nonpassing_with_status_storage_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    watcher: Watcher,
    dry_run: bool,
) -> None:
    (workspace / "accounts/acme-corp/pursuits/broken.md").write_text("---\nstage: [\n---\n", encoding="utf-8")
    failed = _fail_status_replace(monkeypatch)
    result = _run(watcher, dry_run=dry_run)
    assert result == WatcherRunResult("partial" if dry_run else "fatal", True, "skipped" if dry_run else "failed")
    assert len(failed) == int(not dry_run)
    if watcher != "pursuit-stalls":
        report = json.loads(capsys.readouterr().out)
        assert report["outcome"] == ("partial" if dry_run else "fatal")
        assert report["failures"] == (1 if dry_run else 2)


@pytest.mark.parametrize("prior", ["ok", "partial", "fatal"])
def test_pursuit_daily_skip_preserves_nonpassing_prior_outcomes(workspace: Path, prior: status.WatcherOutcome) -> None:
    written = real_write_run_status(
        watcher="pursuit-stalls",
        outcome=prior,
        records_checked=2,
        alerts_generated=1,
        failures=int(prior != "ok"),
        elapsed_seconds=1.0,
        dry_run=False,
    )
    assert written == "written"
    path = workspace / "watchers/watcher-run-status.json"
    before = path.read_bytes()
    result = _run("pursuit-stalls")
    assert result == WatcherRunResult(prior, False, "written" if prior == "ok" else None)
    if prior != "ok":
        assert path.read_bytes() == before


def test_pursuit_daily_success_skip_status_failure_is_nonpassing(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = real_write_run_status(
        watcher="pursuit-stalls",
        outcome="ok",
        records_checked=1,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=1.0,
        dry_run=False,
    )
    assert written == "written"
    path = workspace / "watchers/watcher-run-status.json"
    before = path.read_bytes()
    failed = _fail_status_replace(monkeypatch)
    result = _run("pursuit-stalls")
    assert result == WatcherRunResult("fatal", False, "failed")
    assert failed == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("prior", ["unknown", None])
def test_pursuit_unknown_daily_outcome_is_nonpassing_without_replacing_status(
    workspace: Path, prior: str | None
) -> None:
    written = real_write_run_status(
        watcher="pursuit-stalls",
        outcome="ok",
        records_checked=1,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=1.0,
        dry_run=False,
    )
    assert written == "written"
    path = workspace / "watchers/watcher-run-status.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    if prior is None:
        del saved["pursuit-stalls"]["outcome"]
    else:
        saved["pursuit-stalls"]["outcome"] = prior
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()

    result = _run("pursuit-stalls")

    assert result == WatcherRunResult("fatal", False, None)
    assert not result.completed_partial
    assert path.read_bytes() == before
