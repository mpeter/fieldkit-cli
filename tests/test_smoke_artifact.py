"""Contracts for installed-artifact smoke evidence."""

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest

from fieldkit.gmail.publication import open_gmail_publication
from scripts import release_consumer
from scripts.check_documentation_contract import fenced_blocks

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "smoke_artifact.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_artifact", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def test_unattested_smoke_report_never_passes() -> None:
    report = runner.SmokeReport(
        2, None, "candidate.whl", "a" * 64, "base", "3.11.0", "test", (runner.SmokeCriterion("SMOKE001", "pass"),)
    )
    assert report.ok is False
    assert report.to_dict()["status"] == "diagnostic"
    assert report.to_dict()["source_binding"] == "unattested-dirty"
    assert report.to_dict()["source_revision"] is None


def test_dirty_diagnostic_smoke_does_not_resolve_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"artifact")

    def forbidden_revision(_repo: Path) -> str:
        pytest.fail("dirty diagnostic smoke must not bind HEAD")

    monkeypatch.setattr(runner, "_revision", forbidden_revision)
    monkeypatch.setattr(runner, "_run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "stop"))
    report = runner.smoke(artifact, expected_version="1.0.0", diagnostic_dirty=True)
    assert report.ok is False
    assert report.source_revision is None
    assert report.artifact_sha256 == hashlib.sha256(b"artifact").hexdigest()
    assert report.criteria


def test_dirty_diagnostic_smoke_rejects_attestation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot claim a source revision"):
        runner.smoke(tmp_path / "candidate.whl", source_revision="a" * 40, diagnostic_dirty=True)


def _verified_smoke_dependencies(artifact: Path) -> release_consumer.OfflineDependencies:
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    requirements = artifact.parent / "requirements.txt"
    requirements.write_text(f"candidate==1.0 --hash=sha256:{digest}\n", encoding="utf-8")
    pinned = release_consumer.DownloadedArtifact(requirements, hashlib.sha256(requirements.read_bytes()).hexdigest())
    return release_consumer.OfflineDependencies(
        release_consumer.ExpectedArtifact("wheel", artifact.name, digest),
        (release_consumer.DownloadedArtifact(artifact, digest),),
        pinned,
        pinned,
    )


@pytest.mark.parametrize("invalid", ["fifo", "oversized"])
def test_smoke_offline_preflights_artifact_before_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"artifact")
    offline = _verified_smoke_dependencies(artifact)
    if invalid == "fifo":
        artifact.unlink()
        os.mkfifo(artifact)
    else:
        monkeypatch.setattr(release_consumer, "MAX_ARTIFACT_BYTES", 1)
    read_bytes = Path.read_bytes

    def source_read_is_forbidden(path: Path) -> bytes:
        assert path != artifact, "source must be opened only by the bounded regular-file snapshot"
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", source_read_is_forbidden)
    with pytest.raises(ValueError, match="bounded regular"):
        runner.smoke(artifact, source_revision="a" * 40, expected_version="1.0.0", offline_dependencies=offline)


def test_smoke_offline_reports_snapshot_digest_after_source_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"artifact")
    offline = _verified_smoke_dependencies(artifact)
    snapshot = release_consumer.snapshot_offline_closure

    def replace_source_after_snapshot(
        source: Path, dependencies: release_consumer.OfflineDependencies, destination: Path
    ) -> release_consumer.LocalClosure:
        closure = snapshot(source, dependencies, destination)
        source.write_bytes(b"replacement")
        return closure

    monkeypatch.setattr(release_consumer, "snapshot_offline_closure", replace_source_after_snapshot)
    monkeypatch.setattr(runner, "_run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "stop"))
    report = runner.smoke(artifact, source_revision="a" * 40, expected_version="1.0.0", offline_dependencies=offline)
    assert report.artifact_sha256 == offline.artifact.sha256
    assert report.artifact_name == artifact.name
    assert report.artifact_sha256 != hashlib.sha256(artifact.read_bytes()).hexdigest()


@pytest.mark.parametrize("installer", ["pip", "uv"])
@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_smoke_offline_install_and_tool_use_verified_private_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installer: Literal["pip", "uv"], kind: Literal["wheel", "sdist"]
) -> None:
    artifact = tmp_path / ("candidate.whl" if kind == "wheel" else "candidate.tar.gz")
    artifact.write_bytes(b"artifact")
    wheel = tmp_path / "dependency-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"dependency==1.0 --hash=sha256:{hashlib.sha256(b'wheel').hexdigest()}\n", encoding="utf-8")
    pinned = release_consumer.DownloadedArtifact(requirements, hashlib.sha256(requirements.read_bytes()).hexdigest())
    offline = release_consumer.OfflineDependencies(
        release_consumer.ExpectedArtifact(kind, artifact.name, hashlib.sha256(b"artifact").hexdigest()),
        (release_consumer.DownloadedArtifact(wheel, hashlib.sha256(b"wheel").hexdigest()),),
        pinned,
        pinned,
    )
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        assert not cwd.is_relative_to(_REPO_ROOT)
        assert env["UV_OFFLINE"] == "1"
        assert env["UV_NO_CACHE"] == env["UV_NO_CONFIG"] == "1"
        assert env["UV_PYTHON_DOWNLOADS"] == "never"
        assert env["PIP_CONFIG_FILE"] == os.devnull
        if argv[1:3] == ["tool", "install"]:
            private_artifact = Path(argv[-1])
            assert private_artifact.read_bytes() == b"artifact"
            assert private_artifact != artifact
            raise RuntimeError("captured offline tool install")
        return subprocess.CompletedProcess(argv, 0, "fieldkit 1.0.0\n", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/uv")
    with pytest.raises(RuntimeError, match="captured offline tool install"):
        runner.smoke(
            artifact,
            source_revision="a" * 40,
            expected_version="1.0.0",
            installer=installer,
            offline_dependencies=offline,
        )
    installs = [argv for argv, _env in calls if "install" in argv]
    runtime = installs[0]
    assert "--require-hashes" in runtime and "--no-index" in runtime
    assert Path(runtime[runtime.index("--find-links") + 1]).name == "wheels"
    package = installs[-2]
    assert "--no-deps" in package and "--no-index" in package
    assert ("--no-build-isolation" in package) is (kind == "sdist")
    tool = installs[-1]
    assert tool[tool.index("--python") + 1] == sys.executable
    assert {
        "--offline",
        "--no-cache",
        "--no-config",
        "--no-index",
        "--no-python-downloads",
        "--constraints",
        "--build-constraints",
    } <= set(tool)
    assert Path(tool[tool.index("--constraints") + 1]).name == "runtime-requirements.txt"


def test_smoke_offline_digest_failure_executes_no_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"artifact")
    pinned = release_consumer.DownloadedArtifact(artifact, "0" * 64)
    offline = release_consumer.OfflineDependencies(
        release_consumer.ExpectedArtifact("wheel", artifact.name, "0" * 64),
        (pinned,),
        pinned,
        pinned,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_run", lambda argv, **_kwargs: calls.append(argv))
    with pytest.raises(ValueError, match="digest mismatch"):
        runner.smoke(artifact, source_revision="a" * 40, expected_version="1.0.0", offline_dependencies=offline)
    assert calls == []


def test_gmail_fixture_publishes_rows_for_managed_readers(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "FIELDKIT_DATA_DIR": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "HOME": str(tmp_path),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", runner.GMAIL_SETUP_PROGRAM],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == database
    with closing(open_gmail_publication(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert (
            connection.execute("SELECT value FROM sync_state WHERE key = 'fieldkit_query_ready'").fetchone()[0]
            == "true"
        )
    meeting = subprocess.run(
        [sys.executable, "-c", runner.GMAIL_MEETING_SETUP_PROGRAM],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert meeting.returncode == 0, meeting.stderr
    with closing(open_gmail_publication(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        assert connection.execute("SELECT display_name FROM people").fetchone()[0] == "Casey Customer"


def _run_checkout_cli(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "fieldkit", *argv[1:]] if Path(argv[0]).name == "fieldkit" else argv
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=timeout)


@pytest.mark.integration
def test_local_smoke_uses_live_watcher_logs_and_documented_no_ai_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "FIELDKIT_DATA_DIR": str(tmp_path / "data"),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": str(_REPO_ROOT / "src"),
    }
    fieldkit = Path(sys.executable).with_name("fieldkit")
    workspace = tmp_path / "workspace"
    cwd = tmp_path / "run"
    cwd.mkdir()
    monkeypatch.setattr(runner, "_run", _run_checkout_cli)
    setup = runner._pursuit_examples(fieldkit, workspace=workspace, cwd=cwd, env=env)
    assert all(criterion.status == "pass" for criterion in setup), setup

    criteria = runner._local_examples(fieldkit, workspace=workspace, cwd=cwd, env=env)

    assert all(criterion.status == "pass" for criterion in criteria), criteria
    logs = list((workspace / "logs" / "watchers").glob("*.log"))
    assert len(logs) == 1
    assert "dry_run=False" in logs[0].read_text(encoding="utf-8")
    assert not (workspace / "briefs").exists()


@pytest.mark.integration
def test_gmail_recovery_smoke_accepts_private_fail_closed_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_run", _run_checkout_cli)
    env = {"HOME": str(tmp_path / "home"), "PYTHON_DOTENV_DISABLED": "1", "PYTHONPATH": str(_REPO_ROOT / "src")}
    criteria = runner._gmail_recovery_examples(Path("fieldkit"), root=tmp_path / "recovery", cwd=tmp_path, env=env)

    assert tuple(criterion.status for criterion in criteria) == ("pass", "pass", "pass", "pass"), criteria


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "backup",
        "permissions",
        "stop",
        "rebuild",
        "private-path",
        "table",
        "content",
        "write",
        "stderr",
        "exit",
        "generic",
    ],
)
def test_gmail_recovery_smoke_rejects_incomplete_leaking_or_mutating_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
) -> None:
    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        result = _pursuit_smoke_result(argv, cwd, env)
        assert result is not None
        database = Path(env["FIELDKIT_DATA_DIR"]) / "gmail.db"
        if database.parent.name not in {"corrupt", "missing-table"}:
            return result
        output = result.stdout
        for name, phrase in (
            ("backup", "recoverable backup"),
            ("permissions", "path and permissions"),
            ("stop", "stop cache users"),
            ("rebuild", "fieldkit gmail sync"),
        ):
            if mutation == name:
                output = output.replace(phrase, "")
        if mutation == "private-path":
            output += str(database)
        elif mutation == "table":
            output += "people thread_accounts messages"
        elif mutation == "content":
            output += "private mailbox content"
        elif mutation == "generic":
            output = "gmail: UNHEALTHY\n"
        elif mutation == "write":
            database.write_bytes(b"rewritten")
        return _completed(
            argv,
            returncode=0 if mutation == "exit" else 3,
            stdout=output,
            stderr="private diagnostic" if mutation == "stderr" else "",
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._gmail_recovery_examples(Path("fieldkit"), root=tmp_path / "recovery", cwd=tmp_path, env={})

    assert tuple(criterion.status for criterion in criteria[:2]) == ("pass", "pass")
    assert tuple(criterion.status for criterion in criteria[2:]) == (
        ("pass", "pass") if mutation is None else ("fail", "fail")
    )


@pytest.mark.parametrize("state", ["missing", "empty", "corrupt", "missing-table"])
@pytest.mark.parametrize("mutation", ["chmod", "directory", "cwd-write"])
def test_gmail_recovery_smoke_detects_metadata_directory_and_unrelated_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, mutation: str
) -> None:
    cwd = tmp_path / "run"
    cwd.mkdir()

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        result = _pursuit_smoke_result(argv, cwd, env)
        assert result is not None
        directory = Path(env["FIELDKIT_DATA_DIR"])
        if directory.name == state:
            if mutation == "chmod":
                target = directory / "gmail.db" if state != "missing" else directory
                target.chmod(stat.S_IMODE(target.stat().st_mode) ^ stat.S_IRGRP)
            elif mutation == "directory":
                (directory / "unexpected-empty-directory").mkdir()
            else:
                (cwd / "unexpected").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._gmail_recovery_examples(Path("fieldkit"), root=tmp_path / "recovery", cwd=cwd, env={})

    identifier = {"missing": "SMOKE145", "empty": "SMOKE146", "corrupt": "SMOKE147", "missing-table": "SMOKE148"}[state]
    assert {criterion.criterion_id for criterion in criteria if criterion.status == "fail"} == {identifier}


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "preview-write",
        "preview-cwd-write",
        "preview-data-write",
        "prep-exit",
        "prep-write",
        "list",
        "tail",
        "brief",
        "brief-write",
        "brief-cwd-write",
        "brief-data-write",
    ],
)
def test_local_smoke_rejects_bad_log_preparation_output_and_preview_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
) -> None:
    workspace = tmp_path / "pursuit-workspace"
    (workspace / "config").mkdir(parents=True)
    cwd = tmp_path / "run"
    cwd.mkdir()
    runtime_data = tmp_path / "runtime-data"
    runtime_data.mkdir()
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        result = _pursuit_smoke_result(argv, cwd, env) or _completed(argv)
        if argv[1:] == ["watch", "run", "pursuit-stalls", "--force"]:
            if mutation == "prep-exit":
                return _completed(argv, returncode=3, stderr="preparation failed")
            if mutation == "prep-write":
                (workspace / "unexpected").write_text("changed", encoding="utf-8")
        elif argv[1:] == ["watch", "run", "pursuit-stalls", "--dry-run"] and mutation == "preview-write":
            (workspace / "unexpected").write_text("changed", encoding="utf-8")
        elif argv[1:] == ["watch", "run", "pursuit-stalls", "--dry-run"] and mutation in {
            "preview-cwd-write",
            "preview-data-write",
        }:
            target = cwd if mutation == "preview-cwd-write" else runtime_data
            (target / "unexpected").write_text("changed", encoding="utf-8")
        elif argv[1:] == ["watch", "logs", "--list"] and mutation == "list":
            return _completed(argv, stdout="No log files found.\n")
        elif argv[1:4] == ["watch", "logs", "pursuit-stalls"] and mutation == "tail":
            return _completed(argv, stdout="Run failed\n")
        elif argv[1:3] == ["brief", "generate"]:
            if mutation == "brief":
                return _completed(argv, stdout="[LLM STUB] FIELDKIT_NO_LLM=1 is set — no API call was made.")
            if mutation == "brief-write":
                (workspace / "unexpected").write_text("changed", encoding="utf-8")
            if mutation in {"brief-cwd-write", "brief-data-write"}:
                target = cwd if mutation == "brief-cwd-write" else runtime_data
                (target / "unexpected").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._local_examples(
        Path("fieldkit"), workspace=workspace, cwd=cwd, env={"FIELDKIT_DATA_DIR": str(runtime_data)}
    )

    expected_failures = {
        None: set(),
        "preview-write": {"SMOKE133"},
        "preview-cwd-write": {"SMOKE133"},
        "preview-data-write": {"SMOKE133"},
        "prep-exit": {"SMOKE136", "SMOKE137"},
        "prep-write": {"SMOKE136", "SMOKE137"},
        "list": {"SMOKE136"},
        "tail": {"SMOKE137"},
        "brief": {"SMOKE138"},
        "brief-write": {"SMOKE138"},
        "brief-cwd-write": {"SMOKE138"},
        "brief-data-write": {"SMOKE138"},
    }
    assert {criterion.criterion_id for criterion in criteria if criterion.status == "fail"} == expected_failures[
        mutation
    ]
    assert calls.index(["fieldkit", "watch", "status", "--json"]) < calls.index(
        ["fieldkit", "watch", "run", "pursuit-stalls", "--force"]
    )
    assert calls.index(["fieldkit", "watch", "run", "pursuit-stalls", "--force"]) < calls.index(
        ["fieldkit", "watch", "logs", "--list"]
    )


_QUOTA_OUTPUT = """Quota Gap (2026-H2)
  Weighted pipeline                     : $0
  Closed-won (configured pursuits only) : $0
  Quota target                          : $5,000,000

  Attainment gap: n/a — pursuit-scope closed-won is not comparable
  to a full-book quota. Pass --source sf to pull live
  territory-scoped attainment from Salesforce.
"""
_FORECAST_OUTPUT = """Scenarios:
  Closed Won   :           $0
  Commit       :     $800,000  (closed-won + negotiate)
  Weighted     :     $885,000  (probability-weighted)
  Best Case    :   $1,370,000  (pipeline total, active deals excl. closed-won)
"""

_DISABLED_DOCTOR_OUTPUT = """sf: optional — not configured (no Salesforce session cookie found)
gmail: optional — not configured (database not found)
google: optional — not configured (token file not found)
shadowbot: optional — not configured (no token found)
"""

_UNREADABLE_GMAIL_OUTPUT = (
    "gmail: UNHEALTHY — The cache could not be verified without application writes — preserve it, stop cache users, "
    "and check its path and permissions; "
    "if damaged, stop cache users and move a recoverable backup aside before running 'fieldkit gmail sync'\n"
)

_PACKAGED_SKILL_OUTPUT = """NAME                          E  DESCRIPTION
----------------------------  -  ------------------------------------------------------------
brief                         ✓  Build a local brief
meeting                       ✓  Prepare for a meeting
pursuit-auditor               ✓  Audit pursuit records
task-management               ✓  Manage local tasks

24 skills available (20 with evals).  E=has evals.  Use `fieldkit skill show <name>` for details.
"""


@pytest.mark.parametrize("failure", [None, "exit", "missing", "enabled", "stderr"])
def test_first_use_doctor_requires_all_services_disabled_and_clean_stderr(failure: str | None) -> None:
    output = _DISABLED_DOCTOR_OUTPUT
    if failure == "missing":
        output = output.replace("google: optional — not configured (token file not found)\n", "")
    elif failure == "enabled":
        output = output.replace("sf: optional — not configured", "sf: OK")
    result = _completed(
        ["fieldkit", "doctor"],
        returncode=1 if failure == "exit" else 0,
        stdout=output,
        stderr="warning\n" if failure == "stderr" else "",
    )

    criterion = runner._first_use_doctor_criterion(result)

    assert criterion.criterion_id == "SMOKE151"
    assert criterion.status == ("pass" if failure is None else "fail")


@pytest.mark.parametrize("failure", [None, "exit", "missing", "empty", "stderr"])
def test_first_use_skill_list_requires_known_packaged_skills_and_clean_stderr(failure: str | None) -> None:
    output = _PACKAGED_SKILL_OUTPUT
    if failure == "missing":
        output = output.replace("meeting                       ✓  Prepare for a meeting\n", "")
    elif failure == "empty":
        output = "No skills found.\n"
    result = _completed(
        ["fieldkit", "skill", "list"],
        returncode=1 if failure == "exit" else 0,
        stdout=output,
        stderr="warning\n" if failure == "stderr" else "",
    )

    criterion = runner._first_use_skill_list_criterion(result)

    assert criterion.criterion_id == "SMOKE152"
    assert criterion.status == ("pass" if failure is None else "fail")


@pytest.mark.parametrize("failure", [None, "init", "roots", "home-write"])
def test_getting_started_trial_is_ordered_isolated_and_resolves_trial_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    normal_home = tmp_path / "normal-home"
    normal_home.mkdir()
    cwd = tmp_path / "run"
    cwd.mkdir()
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        trial_root = tmp_path / "getting-started-trial"
        assert cwd == tmp_path / "run"
        assert env["XDG_CONFIG_HOME"] == str(trial_root / "config")
        assert env["FIELDKIT_DATA_DIR"] == str(trial_root / "runtime-data")
        assert env["PYTHON_DOTENV_DISABLED"] == "1"
        assert "GOOGLE_OAUTH_CLIENT_ID" not in env
        assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env
        assert "FIELDKIT_SKILLS_DIR" not in env
        if argv[1:3] == ["init", "--minimal"]:
            if failure == "init":
                return _completed(argv, returncode=3, stderr="invalid setup\n")
            workspace = Path(argv[-1])
            workspace.mkdir(parents=True)
            config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(f"fieldkit_home: {workspace}\n", encoding="utf-8")
            if failure == "home-write":
                (normal_home / "unexpected").write_text("changed", encoding="utf-8")
            return _completed(argv)
        if argv[1:] == ["doctor"]:
            return _completed(argv, stdout=_DISABLED_DOCTOR_OUTPUT)
        if argv[1:] == ["skill", "list"]:
            return _completed(argv, stdout=_PACKAGED_SKILL_OUTPUT)
        assert argv[1] == "-c"
        resolved_workspace = trial_root / ("wrong" if failure == "roots" else "workspace")
        return _completed(
            argv,
            stdout=json.dumps(
                {
                    "config": str(trial_root / "config" / "fieldkit" / "config.yaml"),
                    "data": str(trial_root / "runtime-data"),
                    "workspace": str(resolved_workspace),
                },
                sort_keys=True,
            )
            + "\n",
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._getting_started_trial(
        Path("fieldkit"),
        python=Path("python"),
        root=tmp_path,
        cwd=cwd,
        env={
            "HOME": str(normal_home),
            "XDG_CONFIG_HOME": str(normal_home / ".config"),
            "FIELDKIT_DATA_DIR": str(normal_home / "data"),
            "PYTHON_DOTENV_DISABLED": "1",
            "GOOGLE_OAUTH_CLIENT_ID": "must-clear",
            "GOOGLE_OAUTH_CLIENT_SECRET": "must-clear",
            "FIELDKIT_SKILLS_DIR": "must-clear",
        },
    )

    assert tuple(criterion.criterion_id for criterion in criteria) == ("SMOKE155", "SMOKE151", "SMOKE152")
    assert criteria[0].status == ("pass" if failure is None else "fail")
    if failure == "init":
        assert len(calls) == 1
        assert criteria[1].status == criteria[2].status == "fail"
    else:
        assert [argv[1:] for argv in calls[:3]] == [
            ["init", "--minimal", str(tmp_path / "getting-started-trial" / "workspace")],
            ["doctor"],
            ["skill", "list"],
        ]


def _trial_recipe_commands(document: Path, trial_root: Path) -> list[list[str]]:
    blocks = [block for block in fenced_blocks(document) if "FIELDKIT_TRIAL_ROOT" in block.body]
    assert len(blocks) == 1, "trial recipe must have exactly one trial fence"
    block = blocks[0]
    assert block.language == "console", "trial recipe must be a console fence"
    lines = [line.strip() for line in block.body.splitlines()]
    assert lines == [
        'FIELDKIT_TRIAL_ROOT="$(mktemp -d)"',
        "(",
        'export XDG_CONFIG_HOME="$FIELDKIT_TRIAL_ROOT/config"',
        'export FIELDKIT_DATA_DIR="$FIELDKIT_TRIAL_ROOT/runtime-data"',
        "export PYTHON_DOTENV_DISABLED=1",
        "unset GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET",
        "unset FIELDKIT_SKILLS_DIR",
        'fieldkit init --minimal "$FIELDKIT_TRIAL_ROOT/workspace"',
        "fieldkit doctor",
        "fieldkit skill list",
        ")",
        'echo "$FIELDKIT_TRIAL_ROOT"',
    ], "trial recipe changed its supported setup, isolation, or ordered commands"
    return [
        [*lines[7].split()[:3], str(trial_root / "workspace")],
        lines[8].split(),
        lines[9].split(),
    ]


@pytest.mark.parametrize("mutation", [None, "outside-unset", "unknown-command", "reordered"])
def test_trial_recipe_binding_rejects_changes(tmp_path: Path, mutation: str | None) -> None:
    recipe = (_REPO_ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    if mutation == "outside-unset":
        recipe = recipe.replace("  unset FIELDKIT_SKILLS_DIR\n", "") + "\nunset FIELDKIT_SKILLS_DIR\n"
    elif mutation == "unknown-command":
        recipe = recipe.replace("  fieldkit doctor\n", "  fieldkit nonexistent-trial-command\n")
    elif mutation == "reordered":
        recipe = recipe.replace(
            "  fieldkit doctor\n  fieldkit skill list\n", "  fieldkit skill list\n  fieldkit doctor\n"
        )
    document = tmp_path / "recipe.md"
    document.write_text(recipe, encoding="utf-8")
    if mutation is not None:
        with pytest.raises(AssertionError, match="trial recipe"):
            _trial_recipe_commands(document, tmp_path)
    else:
        commands = _trial_recipe_commands(document, tmp_path)
        assert commands == [
            ["fieldkit", "init", "--minimal", str(tmp_path / "workspace")],
            ["fieldkit", "doctor"],
            ["fieldkit", "skill", "list"],
        ]


@pytest.mark.integration
def test_isolated_trial_clears_external_skill_selector_in_recipe_and_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external-skills" / "external-only"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\nname: external-only\ndescription: Synthetic selector\n---\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    cwd = tmp_path / "run"
    home.mkdir()
    cwd.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "recipe-config"),
        "FIELDKIT_DATA_DIR": str(tmp_path / "recipe-data"),
        "FIELDKIT_SKILLS_DIR": str(external.parent),
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "PYTHON_DOTENV_DISABLED": "1",
    }

    def run_cli(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "fieldkit":
            argv = [sys.executable, "-m", "fieldkit", *argv[1:]]
        return _run_checkout_cli(argv, cwd=cwd, env=env, timeout=timeout)

    dirty = run_cli(["fieldkit", "skill", "list"], cwd=cwd, env=env)
    assert dirty.returncode == 0, dirty.stderr
    assert "external-only" in dirty.stdout
    trial_root = tmp_path / "published-trial"
    commands = _trial_recipe_commands(_REPO_ROOT / "docs" / "getting-started.md", trial_root)
    recipe_env = {
        **env,
        "XDG_CONFIG_HOME": str(trial_root / "config"),
        "FIELDKIT_DATA_DIR": str(trial_root / "runtime-data"),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    for variable in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "FIELDKIT_SKILLS_DIR"):
        recipe_env.pop(variable, None)
    initialized = run_cli(commands[0], cwd=cwd, env=recipe_env)
    assert initialized.returncode == 0, initialized.stderr
    doctor = run_cli(commands[1], cwd=cwd, env=recipe_env)
    assert doctor.returncode == 0, doctor.stderr
    assert runner._first_use_doctor_criterion(doctor).status == "pass"
    clean = run_cli(commands[2], cwd=cwd, env=recipe_env)
    assert clean.returncode == 0, clean.stderr
    assert "external-only" not in clean.stdout
    assert runner._first_use_skill_list_criterion(clean).status == "pass"

    env["FIELDKIT_SKILLS_DIR"] = str(external.parent)
    monkeypatch.setattr(runner, "_run", run_cli)
    criteria = runner._getting_started_trial(
        Path("fieldkit"), python=Path(sys.executable), root=tmp_path, cwd=cwd, env=env
    )
    assert tuple(criterion.status for criterion in criteria) == ("pass", "pass", "pass")


@pytest.mark.parametrize(
    "failure", [None, "setup", "contact-output", "contact-write", "slack-count", "ingest-output", "stderr", "write"]
)
def test_meeting_and_ingest_examples_execute_fixed_argv_without_persistent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    workspace = tmp_path / "workspace"
    database = workspace / "data" / "gmail.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE threads(thread_id TEXT PRIMARY KEY, subject TEXT);
            CREATE TABLE messages(
                message_id TEXT PRIMARY KEY,
                thread_id TEXT,
                from_addr TEXT,
                to_addr TEXT,
                cc_addr TEXT,
                subject TEXT,
                date_epoch INTEGER,
                labels TEXT,
                body_plain TEXT DEFAULT '',
                body_html TEXT DEFAULT ''
            );
            CREATE TABLE people(
                email TEXT PRIMARY KEY,
                display_name TEXT,
                first_seen TEXT,
                last_seen TEXT,
                message_count INTEGER DEFAULT 0,
                thread_count INTEGER DEFAULT 0,
                initiated_count INTEGER DEFAULT 0,
                domain TEXT,
                account TEXT,
                is_internal INTEGER DEFAULT 0,
                meeting_count INTEGER DEFAULT 0,
                slack_user_id TEXT,
                slack_message_count INTEGER DEFAULT 0
            );
            """
        )
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout
        calls.append(argv)
        if argv[1:2] == ["-c"]:
            return _completed(
                argv,
                returncode=3 if failure == "setup" else 0,
                stderr="publication failed" if failure == "setup" else "",
            )
        if argv[1:3] == ["contact", "find"]:
            payload = {
                "type": "resolved",
                "email": "contact@acme-corp.example.com",
                "display_name": "Casey Customer",
                "account": "acme-corp",
                "is_internal": 0,
            }
            if failure == "contact-output":
                payload["account"] = "wrong-account"
            if failure == "slack-count":
                payload["slack_message_count"] = 0
            if failure == "contact-write":
                (tmp_path / "runtime-leak").write_text("unexpected", encoding="utf-8")
            return _completed(
                argv,
                stdout=json.dumps(payload) + "\n",
                stderr="warning\n" if failure == "stderr" else "",
            )
        if failure == "write":
            (workspace / "data" / "pipeline.db").write_text("unexpected", encoding="utf-8")
        output = "[dry-run] Pipeline 'transcript-ingest': 1 source(s) would be registered from gmail.db.\n"
        output += '  [dry-run] doc-fixture-1  (Notes: "Acme review" September 27, 2026)\n'
        if failure == "ingest-output":
            output = "[dry-run] Pipeline 'transcript-ingest': 0 source(s) would be registered from gmail.db.\n"
        return _completed(argv, stdout=output)

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._meeting_ingest_examples(
        Path("fieldkit"),
        python=Path("installed-python"),
        workspace=workspace,
        state_root=tmp_path,
        cwd=tmp_path,
        env={"PYTHONPATH": "network-guard"},
    )

    assert tuple(criterion.criterion_id for criterion in criteria) == ("SMOKE156", "SMOKE157")
    if failure == "setup":
        assert tuple(criterion.status for criterion in criteria) == ("fail", "fail")
        assert calls == [["installed-python", "-c", runner.GMAIL_MEETING_SETUP_PROGRAM]]
        return
    assert calls == [
        ["installed-python", "-c", runner.GMAIL_MEETING_SETUP_PROGRAM],
        ["fieldkit", "contact", "find", "contact@acme-corp.example.com", "--json"],
        [
            "fieldkit",
            "ingest",
            "discover",
            "--pipeline",
            "transcript-ingest",
            "--account",
            "acme-corp",
            "--dry-run",
        ],
    ]
    assert tuple(criterion.criterion_id for criterion in criteria) == ("SMOKE156", "SMOKE157")
    assert criteria[0].status == (
        "fail" if failure in {"contact-output", "contact-write", "slack-count", "stderr"} else "pass"
    )
    assert criteria[1].status == ("fail" if failure in {"ingest-output", "write"} else "pass")


@pytest.mark.parametrize("failure", [None, "readme-init", "readme-config", "user-config", "user-health"])
def test_published_first_use_sequences_are_ordered_and_share_only_their_own_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append((argv, cwd, env))
        arguments = argv[1:]
        if arguments == ["init", "--minimal", "./fieldkit-workspace"]:
            if failure == "readme-init" and cwd.parent.name == "readme-init":
                return _completed(argv, returncode=1, stderr="init failed\n")
            (cwd / "fieldkit-workspace").mkdir()
            config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            expected_config_failure = "readme-config" if cwd.parent.name == "readme-init" else "user-config"
            if failure != expected_config_failure:
                config.write_text(f"fieldkit_root: {cwd / 'fieldkit-workspace'}\n", encoding="utf-8")
            return _completed(argv)
        if arguments == ["doctor"]:
            return _completed(argv, stdout=_DISABLED_DOCTOR_OUTPUT)
        if arguments == ["skill", "list"]:
            return _completed(argv, stdout=_PACKAGED_SKILL_OUTPUT)
        if arguments == ["--help"]:
            return _completed(argv, stdout="Usage: fieldkit [OPTIONS] COMMAND [ARGS]...\n")
        if arguments == ["pursuit", "health"]:
            return _completed(
                argv,
                returncode=1 if failure == "user-health" else 3,
                stdout="No pursuit files found.\n",
            )
        if arguments == ["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"]:
            return _completed(
                argv,
                stdout=(
                    "## ☀️ Morning Brief — 2026-09-27\n\n## LLM Sections (omitted — run without --no-llm to generate)\n"
                ),
                stderr="[morning_brief] Collecting data for 2026-09-27 ...\n",
            )
        if arguments == ["-c", runner._TRIAL_ROOT_PROGRAM]:
            scenario_root = cwd.parent
            return _completed(
                argv,
                stdout=json.dumps(
                    {
                        "config": str(scenario_root / "config" / "fieldkit" / "config.yaml"),
                        "data": str(scenario_root / "runtime-data"),
                        "workspace": str(cwd / "fieldkit-workspace"),
                    },
                    sort_keys=True,
                )
                + "\n",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._published_first_use_sequences(
        Path("fieldkit"), root=tmp_path, env={"HOME": str(tmp_path / "normal-home"), "PYTHONPATH": "guard"}
    )

    assert tuple(criterion.criterion_id for criterion in criteria) == ("SMOKE158", "SMOKE159", "SMOKE160")
    assert criteria[0].status == ("fail" if failure in {"readme-init", "readme-config"} else "pass")
    assert criteria[1].status == ("fail" if failure == "user-config" else "pass")
    assert criteria[2].status == ("fail" if failure in {"user-config", "user-health"} else "pass")
    assert [(argv[1:], cwd.parent.name) for argv, cwd, _ in calls] == [
        (["init", "--minimal", "./fieldkit-workspace"], "readme-init"),
        *(
            []
            if failure == "readme-init"
            else [
                (["doctor"], "readme-init"),
                (["skill", "list"], "readme-init"),
                (["-c", runner._TRIAL_ROOT_PROGRAM], "readme-init"),
            ]
        ),
        (["init", "--minimal", "./fieldkit-workspace"], "user-guide"),
        (["--help"], "user-guide"),
        (["skill", "list"], "user-guide"),
        (["-c", runner._TRIAL_ROOT_PROGRAM], "user-guide"),
        (["doctor"], "user-guide"),
        (["pursuit", "health"], "user-guide"),
        (["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"], "user-guide"),
    ]
    for _, cwd, command_env in calls:
        assert command_env["XDG_CONFIG_HOME"] == str(cwd.parent / "config")
        assert command_env["FIELDKIT_DATA_DIR"] == str(cwd.parent / "runtime-data")
        assert command_env["PYTHON_DOTENV_DISABLED"] == "1"
        assert all(variable not in command_env for variable in runner._TRIAL_CREDENTIAL_ENV)


@pytest.mark.parametrize(
    "failure", [None, "exit", "output", "stderr", "write", "directory", "symlink", "mode", "config"]
)
def test_empty_pursuit_health_requires_exact_output_and_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unchanged = workspace / "unchanged.txt"
    unchanged.write_text("original", encoding="utf-8")
    unchanged.chmod(0o600)
    environment = {"PYTHONPATH": "network-guard"}

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        assert argv == ["fieldkit", "pursuit", "health"]
        assert cwd == tmp_path and env == environment
        assert timeout == runner.COMMAND_TIMEOUT_SECONDS
        if failure == "write":
            (workspace / "unexpected.txt").write_text("changed", encoding="utf-8")
        if failure == "directory":
            (workspace / "unexpected").mkdir()
        if failure == "symlink":
            (workspace / "redirect").symlink_to(tmp_path / "outside")
        if failure == "mode":
            unchanged.chmod(0o644)
        if failure == "config":
            (tmp_path / "unexpected-config.yaml").write_text("changed", encoding="utf-8")
        return _completed(
            argv,
            returncode=1 if failure == "exit" else 3,
            stdout="No pursuit files found.\n" if failure != "output" else "unrelated output\n",
            stderr="warning" if failure == "stderr" else "",
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criterion = runner._empty_pursuit_health(Path("fieldkit"), state_root=tmp_path, cwd=tmp_path, env=environment)

    assert criterion.criterion_id == "SMOKE150"
    assert criterion.status == ("pass" if failure is None else "fail")


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "setup",
        "audit-exit",
        "missing-report",
        "empty-report",
        "audit-write",
        "preview-exit",
        "preview-output",
        "preview-write",
    ],
)
def test_pursuit_examples_require_report_and_unchanged_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    workspace = tmp_path / "pursuit-workspace"
    calls: list[list[str]] = []
    environment = {"PYTHONPATH": str(tmp_path / "network-guard")}

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path and env == environment
        assert timeout == runner.COMMAND_TIMEOUT_SECONDS
        calls.append(argv)
        if argv[1:3] == ["init", "--minimal"]:
            workspace.mkdir()
            return _completed(argv, returncode=3 if failure == "setup" else 0)
        pursuit = workspace / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"
        if argv[1:] == ["pursuit", "audit"]:
            if failure != "missing-report":
                report = workspace / "accounts" / ".audit" / "pursuit-compliance-2026-01-01.md"
                report.parent.mkdir()
                report.write_text(
                    "" if failure == "empty-report" else "current qualification: unavailable", encoding="utf-8"
                )
            if failure == "audit-write":
                pursuit.write_text("changed", encoding="utf-8")
            return _completed(argv, returncode=1 if failure == "audit-exit" else 0)
        assert argv[1:] == ["pursuit", "advance", str(pursuit), "--dry-run"]
        if failure == "preview-write":
            (workspace / "unexpected.txt").write_text("changed", encoding="utf-8")
        return _completed(
            argv,
            returncode=0 if failure == "preview-exit" else 1,
            stdout="unrelated"
            if failure == "preview-output"
            else "[dry-run] Gate pending — would NOT advance (use --override REASON to force)\n",
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._pursuit_examples(Path("fieldkit"), workspace=workspace, cwd=tmp_path, env=environment)

    assert tuple(item.criterion_id for item in criteria) == ("SMOKE130", "SMOKE131", "SMOKE132")
    assert criteria[0].status == ("fail" if failure == "setup" else "pass")
    assert criteria[1].status == (
        "fail" if failure in {"audit-exit", "missing-report", "empty-report", "audit-write"} else "pass"
    )
    assert criteria[2].status == ("fail" if failure in {"preview-exit", "preview-output", "preview-write"} else "pass")
    assert calls == [
        ["fieldkit", "init", "--minimal", str(workspace)],
        ["fieldkit", "pursuit", "audit"],
        [
            "fieldkit",
            "pursuit",
            "advance",
            str(workspace / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"),
            "--dry-run",
        ],
    ]


@pytest.mark.parametrize("failure", [None, "setup", "quota-exit", "quota-output", "forecast-exit", "forecast-output"])
def test_pipeline_examples_require_fixed_commands_and_expected_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    calls: list[list[str]] = []
    workspace = tmp_path / "workspace"
    environment = {"PYTHONPATH": str(tmp_path / "network-guard")}

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env == environment
        assert timeout == runner.COMMAND_TIMEOUT_SECONDS
        calls.append(argv)
        if "--set" in argv:
            return _completed(argv, returncode=3 if failure == "setup" else 0)
        if argv[1:] == ["pipeline", "quota"]:
            assert not (workspace / "accounts").exists()
            return _completed(
                argv,
                returncode=3 if failure == "quota-exit" else 0,
                stdout="unrelated" if failure == "quota-output" else _QUOTA_OUTPUT,
            )
        assert argv[1:] == ["pursuit", "forecast"]
        assert len(list(workspace.glob("accounts/*/pursuits/*.md"))) == 3
        return _completed(
            argv,
            returncode=3 if failure == "forecast-exit" else 0,
            stdout=_FORECAST_OUTPUT.replace("$885,000", "$1") if failure == "forecast-output" else _FORECAST_OUTPUT,
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criteria = runner._pipeline_examples(Path("fieldkit"), workspace=workspace, cwd=tmp_path, env=environment)

    assert tuple(item.criterion_id for item in criteria) == ("SMOKE127", "SMOKE128", "SMOKE129")
    assert tuple(item.status for item in criteria) == (
        "fail" if failure == "setup" else "pass",
        "fail" if failure in {"quota-exit", "quota-output"} else "pass",
        "fail" if failure in {"forecast-exit", "forecast-output"} else "pass",
    )
    assert calls == [
        ["fieldkit", "pipeline", "quota", "--set", "5000000", "--period", "2026-H2"],
        ["fieldkit", "pipeline", "quota"],
        ["fieldkit", "pursuit", "forecast"],
    ]


@pytest.mark.parametrize(
    ("python_output", "uv_output", "returncode", "expected"),
    [
        ("Python 3.11.9\n", "uv 0.8.0\n", 0, ("pass", "pass")),
        ("Python 3.14.0\n", "uv 0.8.0 (abcdef 2026-01-01)\n", 0, ("pass", "pass")),
        ("", "uv 0.8.0\n", 0, ("fail", "pass")),
        ("Python 3.11.9\n", "unrelated output\n", 0, ("pass", "fail")),
        ("Python 3.11.9\n", "uv 0.8.0\n", 1, ("fail", "fail")),
    ],
)
def test_prerequisite_versions_require_success_and_version_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    python_output: str,
    uv_output: str,
    returncode: int,
    expected: tuple[str, str],
) -> None:
    calls: list[list[str]] = []
    environment = {"PATH": "/fixture/bin"}

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env == environment
        assert timeout == runner.COMMAND_TIMEOUT_SECONDS
        calls.append(argv)
        output = python_output if argv[0] == "python3" else uv_output
        return _completed(argv, returncode=returncode, stdout=output)

    monkeypatch.setattr(runner, "_run", fake_run)

    criteria = runner._prerequisite_versions(cwd=tmp_path, env=environment)

    assert tuple(item.status for item in criteria) == expected
    assert tuple(item.criterion_id for item in criteria) == ("SMOKE125", "SMOKE126")
    assert calls == [["python3", "--version"], ["uv", "--version"]]


@pytest.mark.parametrize("failure", [FileNotFoundError("missing executable"), subprocess.TimeoutExpired("uv", 60)])
def test_prerequisite_execution_failure_does_not_become_passing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(runner, "_run", fail_run)

    with pytest.raises(type(failure), match=r"missing executable|timed out"):
        runner._prerequisite_versions(cwd=tmp_path, env={})


def _completed(
    argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.mark.parametrize(
    "mutation",
    [
        "",
        "missing",
        "identity",
        "account",
        "stub",
        "directories",
        "writer-failed",
        "workspace-symlink",
        "config-symlink",
        "account-symlink",
        "wrong-heading",
    ],
)
def test_unattended_init_rejects_incomplete_generated_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        if "--answers" in argv:
            answers = Path(argv[-1]).read_text(encoding="utf-8")
            assert "role: Account Executive\ncompany: Example Company\nterritory: East\n" in answers
            assert 'salesforce_user_id: ""\naccounts:\n  - Acme Corp\n' in answers
            if mutation == "writer-failed":
                return _completed(argv, returncode=3, stderr="writer failed")
            workspace = tmp_path / "answers-workspace"
            workspace.mkdir()
            if mutation != "missing":
                _write_answers_example(workspace, mutation=mutation)
            if mutation.endswith("-symlink"):
                redirected = {
                    "workspace-symlink": workspace,
                    "config-symlink": workspace / "config",
                    "account-symlink": workspace / "accounts" / "acme-corp",
                }[mutation]
                target = tmp_path / "redirected"
                redirected.rename(target)
                redirected.symlink_to(target, target_is_directory=True)
            return _completed(argv)
        return subprocess.run(
            [sys.executable, *argv[1:]], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    criterion = runner._unattended_init(
        Path("fieldkit"), python=Path(sys.executable), root=tmp_path, cwd=tmp_path, env={}
    )
    assert criterion.status == ("pass" if not mutation else "fail")
    assert criterion.criterion_id == "SMOKE123"
    if mutation.endswith("-symlink"):
        assert "directories must be real directories" in criterion.diagnostic


def _write_answers_example(workspace: Path, *, mutation: str = "") -> None:
    config = workspace / "config"
    config.mkdir()
    identity = {
        "name": "Example User",
        "email": "user@example.com",
        "role": "Account Executive",
        "company": "Example Company",
        "territory": "East",
        "salesforce_user_id": "",
        "accounts": ["Acme Corp"],
        "motions": ["Pre-sales pursuit", "Landed account expansion", "Relationship maintenance"],
    }
    if mutation == "identity":
        identity["company"] = "Wrong Company"
    (config / "identity.yaml").write_text(json.dumps({"identity": identity}), encoding="utf-8")
    account = {
        "domains": [],
        "team": [],
        "keywords": ["Acme Corp"],
        "blindspots_min_messages": 20,
        "blindspot_days": 14,
    }
    if mutation == "account":
        account.pop("keywords")
    (config / "accounts.yaml").write_text(
        json.dumps({"internal_domains": [], "accounts": {"acme-corp": account}}), encoding="utf-8"
    )
    directory = workspace / "accounts" / "acme-corp"
    directory.mkdir(parents=True)
    for name in ("pursuits", "meetings", "projects", "proposals"):
        if mutation != "directories" or name != "proposals":
            (directory / name).mkdir()
    if mutation != "stub":
        heading = "# Acme Corporate unrelated" if mutation == "wrong-heading" else "# Acme Corp"
        (directory / "account.md").write_text(f"---\naccount: Acme Corp\n---\n\n{heading}\n", encoding="utf-8")


def test_unattended_init_checks_genuine_writer_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "user-config"),
        "FIELDKIT_DATA_DIR": str(tmp_path / "runtime"),
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "FIELDKIT_NO_LLM": "1",
    }

    def real_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--answers" in argv:
            argv = [sys.executable, "-m", "fieldkit", *argv[1:]]
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)

    monkeypatch.setattr(runner, "_run", real_run)
    criterion = runner._unattended_init(
        Path("fieldkit"), python=Path(sys.executable), root=tmp_path, cwd=tmp_path, env=environment
    )
    assert criterion.status == "pass", criterion.diagnostic
    assert calls[1][1:3] == ["-c", runner._ANSWERS_EXAMPLE_PROGRAM]
    assert len(calls) == 2


def _pursuit_smoke_result(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str] | None:
    if argv[1:3] == ["tool", "install"]:
        (Path(env["UV_TOOL_DIR"]) / "fieldkit-cli").mkdir(parents=True, exist_ok=True)
        return _completed(argv)
    if argv[1:] == ["tool", "uninstall", "fieldkit-cli"]:
        (Path(env["UV_TOOL_DIR"]) / "fieldkit-cli").rmdir()
        return _completed(argv)
    if argv[1:3] == ["init", "--minimal"] and Path(argv[-1]).name == "tool-workspace":
        Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("fieldkit_home: trial\n", encoding="utf-8")
        return _completed(argv)
    if argv[1:3] == ["init", "--minimal"] and Path(argv[-1]).parent.name == "getting-started-trial":
        workspace = Path(argv[-1])
        workspace.mkdir(parents=True)
        config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(f"fieldkit_home: {workspace}\n", encoding="utf-8")
        return _completed(argv)
    if argv[1:] == ["init", "--minimal", "./fieldkit-workspace"] and cwd.parent.name in {
        "readme-init",
        "user-guide",
    }:
        workspace = cwd / "fieldkit-workspace"
        workspace.mkdir()
        config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(f"fieldkit_home: {workspace}\n", encoding="utf-8")
        return _completed(argv)
    if argv[1:2] == ["-c"] and "from fieldkit.config import CONFIG_PATH" in argv[-1]:
        workspace = (
            cwd / "fieldkit-workspace"
            if cwd.parent.name in {"readme-init", "user-guide"}
            else Path(env["XDG_CONFIG_HOME"]).parent / "workspace"
        )
        return _completed(
            argv,
            stdout=json.dumps(
                {
                    "config": str(Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"),
                    "data": env["FIELDKIT_DATA_DIR"],
                    "workspace": str(workspace),
                },
                sort_keys=True,
            )
            + "\n",
        )
    if argv[1:] == ["doctor", "gmail"] and "FIELDKIT_DATA_DIR" in env:
        state = Path(env["FIELDKIT_DATA_DIR"]).name
        output = _UNREADABLE_GMAIL_OUTPUT
        if state == "missing":
            output = "gmail: optional — not configured (run 'fieldkit gmail sync')\n"
        elif state == "empty":
            output = "gmail: UNHEALTHY — The cache is empty — preserve a backup before rebuilding with 'fieldkit gmail sync'\n"
        return _completed(
            argv,
            returncode=3,
            stdout=output,
        )
    workspace = cwd.parent / "pursuit-workspace"
    database = workspace / "data" / "gmail.db"
    if argv[1:2] == ["-c"] and "from fieldkit.gmail.discover import get_gmail_db_path" in argv[-1]:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.executescript((_REPO_ROOT / "src" / "fieldkit" / "gmail" / "schema.sql").read_text(encoding="utf-8"))
        connection.close()
        return _completed(argv, stdout=str(database))
    if argv[1:] == ["gmail", "account-tags"]:
        connection = sqlite3.connect(database)
        connection.executemany(
            "INSERT INTO thread_accounts VALUES (?, ?)",
            [("thread-1", "acme-corp"), ("thread-2", "acme-corp"), ("thread-3", "global-pay")],
        )
        connection.commit()
        connection.close()
        return _completed(
            argv,
            stdout="Upserted 3 thread-account associations across 2 account(s):\n  acme-corp: 2 threads\n  global-pay: 1 threads\n",
        )
    if argv[1:] == ["doctor", "gmail"]:
        return _completed(argv, stdout="gmail: OK — 3 messages, 0.1 MB")
    if argv[1:] == ["gmail", "enrich-pursuits"]:
        (workspace / "accounts" / "acme-corp" / "gmail-intel.md").write_text(
            "# Gmail Intelligence Report: acme-corp\n## Top Contacts by Email Volume\n"
            "| contact@example.com | 2 | 0d ago |\n"
            "## Champion Signals (Top 10 Contacts)\n## Per-Pursuit Thread Matches\n"
            "### acme-corp-q3\nAcme quarterly planning\n## Review Checklist\n"
            "contact@example.com\n",
            encoding="utf-8",
        )
        return _completed(argv, stdout="Done.")
    if argv[1:] == ["contact", "find", "contact@acme-corp.example.com", "--json"]:
        return _completed(
            argv,
            stdout=json.dumps(
                {
                    "type": "resolved",
                    "email": "contact@acme-corp.example.com",
                    "display_name": "Casey Customer",
                    "account": "acme-corp",
                    "is_internal": 0,
                }
            )
            + "\n",
        )
    if argv[1:] == [
        "ingest",
        "discover",
        "--pipeline",
        "transcript-ingest",
        "--account",
        "acme-corp",
        "--dry-run",
    ]:
        return _completed(
            argv,
            stdout=(
                "[dry-run] Pipeline 'transcript-ingest': 1 source(s) would be registered from gmail.db.\n"
                '  [dry-run] doc-fixture-1  (Notes: "Acme review" September 27, 2026)\n'
            ),
        )
    if argv[1:3] == ["init", "--minimal"] and Path(argv[-1]).name == "pursuit-workspace":
        (Path(argv[-1]) / "config").mkdir(parents=True)
        return _completed(argv)
    local_outputs = {
        ("watch", "run", "pursuit-stalls", "--dry-run"): "1 pursuit(s) scanned, 0 stall alert(s) would fire",
        ("watch", "status"): "No watcher runs recorded yet.",
        ("watch", "status", "--json"): '{"items": [], "count": 0, "filters": {}}',
        ("watch", "logs", "--list"): "Recent log files (1):",
        ("watch", "logs", "pursuit-stalls", "--tail", "100"): "Run complete: checked=1 stalled=0",
        (
            "brief",
            "generate",
            "--pipeline-only",
            "--dry-run",
        ): "## ☀️ Morning Brief — 2026-09-29\n"
        "## LLM Sections (omitted — run without --no-llm to generate)\n"
        "### 🚦 Pursuit Alerts\n### 📋 Today's Commitments\n",
        ("doctor",): _DISABLED_DOCTOR_OUTPUT,
        ("skill", "list"): _PACKAGED_SKILL_OUTPUT,
        ("pursuit", "health"): "No pursuit files found.\n",
    }
    if tuple(argv[1:]) in local_outputs:
        return _completed(
            argv,
            returncode=3 if argv[1:] == ["pursuit", "health"] else 0,
            stdout=local_outputs[tuple(argv[1:])],
        )
    if argv[1:] == ["pursuit", "audit"]:
        report = cwd.parent / "pursuit-workspace" / "accounts" / ".audit" / "pursuit-compliance-2026-01-01.md"
        report.parent.mkdir(parents=True)
        report.write_text("current qualification: unavailable", encoding="utf-8")
        return _completed(argv)
    if argv[1:3] == ["pursuit", "advance"]:
        return _completed(
            argv,
            returncode=1,
            stdout="[dry-run] Gate pending — would NOT advance (use --override REASON to force)\n",
        )
    return None


@pytest.mark.parametrize("missing_command", [None, "auth google", "meeting link", "skill eval", "web serve"])
@pytest.mark.parametrize("installer", ["pip", "uv"])
@pytest.mark.parametrize(
    "tool_uninstall", ["removed", "retained", "dangling", "failed", "environment", "persistent", "setup"]
)
def test_smoke_records_exact_digest_environment_and_all_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_command: str | None,
    installer: Literal["pip", "uv"],
    tool_uninstall: str,
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"exact candidate")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd, env))
        if tool_uninstall == "setup" and argv[1:3] == ["init", "--minimal"] and Path(argv[-1]).name == "tool-workspace":
            return _completed(argv, returncode=3, stderr="setup failed\n")
        if argv[1:] == ["tool", "uninstall", "fieldkit-cli"]:
            launcher = Path(env["UV_TOOL_BIN_DIR"]) / "fieldkit"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            if tool_uninstall == "retained":
                launcher.write_text("retained launcher", encoding="utf-8")
            elif tool_uninstall == "dangling":
                launcher.symlink_to(launcher.parent / "missing-target")
            tool_environment = Path(env["UV_TOOL_DIR"]) / "fieldkit-cli"
            if tool_uninstall != "environment":
                tool_environment.rmdir()
            if tool_uninstall == "persistent":
                (Path(env["FIELDKIT_DATA_DIR"]) / "retained.txt").write_text("changed", encoding="utf-8")
            return _completed(argv, returncode=1 if tool_uninstall == "failed" else 0)
        if argv[1:3] == ["tool", "install"]:
            (Path(env["UV_TOOL_DIR"]) / "fieldkit-cli").mkdir(parents=True)
        if argv[1:3] == ["init", "--minimal"] and Path(argv[-1]).name == "tool-workspace":
            Path(argv[-1]).mkdir(parents=True)
            config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("fieldkit_home: trial\n", encoding="utf-8")
        pursuit_result = _pursuit_smoke_result(argv, cwd, env)
        if pursuit_result is not None:
            return pursuit_result
        if argv[1:] == ["pipeline", "quota"]:
            return _completed(argv, stdout=_QUOTA_OUTPUT)
        if argv[1:] == ["pursuit", "forecast"]:
            return _completed(argv, stdout=_FORECAST_OUTPUT)
        if argv == ["python3", "--version"]:
            return _completed(argv, stdout="Python 3.11.9\n")
        if argv == ["uv", "--version"]:
            return _completed(argv, stdout="uv 0.8.0\n")
        if argv[-1] == "--version":
            return _completed(argv, stdout="fieldkit 1.0.0\n")
        if argv[1:] == ["--help"]:
            return _completed(argv, stdout="Usage: fieldkit [OPTIONS] COMMAND [ARGS]...\n")
        if argv[1:] == ["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"]:
            return _completed(
                argv,
                stdout=(
                    "## ☀️ Morning Brief — 2026-09-27\n\n## LLM Sections (omitted — run without --no-llm to generate)\n"
                ),
                stderr="[morning_brief] Collecting data for 2026-09-27 ...\n",
            )
        if argv[-2:] == ["web", "--help"]:
            return _completed(argv, returncode=3, stderr="requires the optional profile")
        if argv[-2:] == ["commands", "--json"]:
            return _completed(
                argv,
                stdout=json.dumps(
                    [
                        {"full_name": name}
                        for name in ("auth google", "meeting link", "skill eval", "web serve")
                        if name != missing_command
                    ]
                ),
            )
        if argv[-3:] == ["--features", "--json"] or argv[-3:] == ["version", "--features", "--json"]:
            return _completed(
                argv,
                stdout=(
                    '{"cli":{"groups":['
                    '{"group":"auth","subcommands":[{"name":"google"},{"name":"sf"},{"name":"shadowbot"}]},'
                    '{"group":"gmail","subcommands":[{"name":"query"},{"name":"sync"}]}'
                    "]}}"
                ),
            )
        if "--behavioral" in argv:
            return _completed(argv, returncode=3, stderr="requires the 'llm' optional profile")
        if argv[-3:] == ["init", "--minimal", "./fieldkit-workspace"]:
            (cwd / "fieldkit-workspace").mkdir()
        if "--answers" in argv:
            answers = Path(argv[-1]).read_text(encoding="utf-8")
            data_dir = next(
                line.removeprefix("data_dir: ") for line in answers.splitlines() if line.startswith("data_dir: ")
            )
            Path(data_dir).mkdir()
        if argv[1:] == ["skill", "install", "--tool", "cursor", "--skill", "brief"]:
            rule = cwd / ".cursor" / "rules" / "brief.md"
            rule.parent.mkdir(parents=True)
            rule.write_text("Bundled support: `ops/week-start.md`\n", encoding="utf-8")
        return _completed(argv)

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/uv")

    report = runner.smoke(
        artifact,
        repo_root=_REPO_ROOT,
        source_revision="a" * 40,
        expected_version="1.0.0",
        installer=installer,
    )

    assert report.ok is (missing_command is None and tool_uninstall == "removed")
    tool_criterion = next(item for item in report.criteria if item.criterion_id == "SMOKE149")
    assert tool_criterion.status == ("pass" if tool_uninstall == "removed" else "fail")
    registry_criterion = next(item for item in report.criteria if item.criterion_id == "SMOKE109")
    assert registry_criterion.status == ("pass" if missing_command is None else "fail")
    assert report.artifact_sha256 == hashlib.sha256(b"exact candidate").hexdigest()
    assert {criterion.criterion_id for criterion in report.criteria} == {
        "SMOKE001",
        "SMOKE002",
        "SMOKE101",
        "SMOKE102",
        "SMOKE103",
        "SMOKE104",
        "SMOKE105",
        "SMOKE106",
        "SMOKE107",
        "SMOKE108",
        "SMOKE109",
        "SMOKE110",
        "SMOKE111",
        "SMOKE112",
        "SMOKE113",
        "SMOKE114",
        "SMOKE115",
        "SMOKE116",
        "SMOKE117",
        "SMOKE118",
        "SMOKE119",
        "SMOKE120",
        "SMOKE121",
        "SMOKE122",
        "SMOKE123",
        "SMOKE124",
        "SMOKE125",
        "SMOKE126",
        "SMOKE127",
        "SMOKE128",
        "SMOKE129",
        "SMOKE130",
        "SMOKE131",
        "SMOKE132",
        "SMOKE133",
        "SMOKE134",
        "SMOKE135",
        "SMOKE136",
        "SMOKE137",
        "SMOKE138",
        "SMOKE139",
        "SMOKE140",
        "SMOKE141",
        "SMOKE142",
        "SMOKE143",
        "SMOKE144",
        "SMOKE145",
        "SMOKE146",
        "SMOKE147",
        "SMOKE148",
        "SMOKE149",
        "SMOKE150",
        "SMOKE151",
        "SMOKE152",
        "SMOKE153",
        "SMOKE154",
        "SMOKE155",
        "SMOKE156",
        "SMOKE157",
        "SMOKE158",
        "SMOKE159",
        "SMOKE160",
    }
    assert calls
    assert all(call_cwd != _REPO_ROOT for _, call_cwd, _ in calls)
    assert all(env["PYTHONSAFEPATH"] == "1" for _, _, env in calls)
    assert all(env["PYTHON_DOTENV_DISABLED"] == "1" for _, _, env in calls)
    assert all(env["PYTHONPATH"] == "" or Path(env["PYTHONPATH"]).name == "network-guard" for _, _, env in calls)
    assert all(Path(env["TMPDIR"]).name == "tmp" for _, _, env in calls)
    assert any(Path(argv[-2]).name == "fieldkit" and argv[-1] == "doctor" for argv, _, _ in calls)
    assert any(argv[-3:] == ["tool", "install", str(artifact.resolve())] for argv, _, _ in calls)
    assert any(argv[-3:] == ["init", "--minimal", "./fieldkit-workspace"] for argv, _, _ in calls)
    assert any(argv[-5:] == ["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"] for argv, _, _ in calls)
    trial_init_index = next(
        index
        for index, (argv, _, _) in enumerate(calls)
        if argv[-2:] == ["--minimal", str(Path(argv[-1]).parent / "workspace")]
        and Path(argv[-1]).parent.name == "getting-started-trial"
    )
    first_use = calls[trial_init_index : trial_init_index + 4]
    assert [argv[1:] for argv, _, _ in first_use] == [
        ["init", "--minimal", str(Path(first_use[0][0][-1]))],
        ["doctor"],
        ["skill", "list"],
        ["-c", runner._TRIAL_ROOT_PROGRAM],
    ]
    assert all(Path(command_env["PYTHONPATH"]).name == "network-guard" for _, _, command_env in first_use)
    trial_env = first_use[0][2]
    trial_root = Path(first_use[0][0][-1]).parent
    assert trial_env["XDG_CONFIG_HOME"] == str(trial_root / "config")
    assert trial_env["FIELDKIT_DATA_DIR"] == str(trial_root / "runtime-data")
    assert all(variable not in trial_env for variable in runner._TRIAL_CREDENTIAL_ENV)
    answers_calls = [argv for argv, _, _ in calls if argv[-2:] and "--answers" in argv]
    assert len(answers_calls) == 1
    assert Path(answers_calls[0][-1]).name == "init-answers.yaml"
    offline_calls = [
        env
        for argv, _, env in calls
        if argv[-3:-1] == ["init", "--minimal"] and Path(argv[-1]).name == "offline-workspace"
    ]
    assert offline_calls and Path(offline_calls[0]["PYTHONPATH"]).name == "network-guard"
    assert any(argv[1:] == ["tool", "uninstall", "fieldkit-cli"] for argv, _, _ in calls)
    uninstall_index = next(
        index for index, (argv, _, _) in enumerate(calls) if "uninstall" in argv and "tool" not in argv
    )
    uninstall_argv = calls[uninstall_index][0]
    if installer == "uv":
        assert uninstall_argv[:4] == ["/usr/bin/uv", "pip", "uninstall", "--python"]
        assert Path(uninstall_argv[4]).parent.parent.name == "venv"
        assert uninstall_argv[5:] == ["fieldkit-cli"]
    else:
        assert uninstall_argv[1:] == ["-m", "pip", "uninstall", "--yes", "fieldkit-cli"]
    assert calls[uninstall_index + 1][0][-1] == str(artifact.resolve())
    assert calls[uninstall_index + 2][0][-1] == "--version"
    live_eval_envs = [env for argv, _, env in calls if "--behavioral" in argv]
    assert live_eval_envs and "FIELDKIT_NO_LLM" not in live_eval_envs[0]


def test_failed_command_has_bounded_diagnostic_without_stdout() -> None:
    result = _completed(["fieldkit"], returncode=7, stdout="ignored", stderr="x" * 800)

    criterion = runner._criterion("SMOKE999", result)

    assert criterion.status == "fail"
    assert len(criterion.diagnostic) == 500
    assert "ignored" not in criterion.diagnostic


def test_all_profile_installs_exact_artifact_extra_and_exercises_each_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"exact candidate")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(argv)
        pursuit_result = _pursuit_smoke_result(argv, cwd, env)
        if pursuit_result is not None:
            return pursuit_result
        if argv == ["python3", "--version"]:
            return _completed(argv, stdout="Python 3.11.9\n")
        if argv == ["uv", "--version"]:
            return _completed(argv, stdout="uv 0.8.0\n")
        if argv[1:] == ["pipeline", "quota"]:
            return _completed(argv, stdout=_QUOTA_OUTPUT)
        if argv[1:] == ["pursuit", "forecast"]:
            return _completed(argv, stdout=_FORECAST_OUTPUT)
        if argv[-1] == "--version":
            return _completed(argv, stdout="fieldkit 1.0.0\n")
        if argv[1:] == ["--help"]:
            return _completed(argv, stdout="Usage: fieldkit [OPTIONS] COMMAND [ARGS]...\n")
        if argv[1:] == ["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"]:
            return _completed(
                argv,
                stdout=(
                    "## ☀️ Morning Brief — 2026-09-27\n\n## LLM Sections (omitted — run without --no-llm to generate)\n"
                ),
                stderr="[morning_brief] Collecting data for 2026-09-27 ...\n",
            )
        if argv[-2:] == ["commands", "--json"]:
            return _completed(
                argv,
                stdout=(
                    '[{"full_name":"auth google"},{"full_name":"meeting link"},'
                    '{"full_name":"skill eval"},{"full_name":"web serve"}]'
                ),
            )
        if argv[-3:] == ["version", "--features", "--json"]:
            return _completed(
                argv,
                stdout=(
                    '{"cli":{"groups":['
                    '{"group":"auth","subcommands":[{"name":"google"},{"name":"sf"},{"name":"shadowbot"}]},'
                    '{"group":"gmail","subcommands":[{"name":"query"},{"name":"sync"}]}'
                    "]}}"
                ),
            )
        if argv[-3:] == ["init", "--minimal", "./fieldkit-workspace"]:
            (cwd / "fieldkit-workspace").mkdir()
        if "--answers" in argv:
            answers = Path(argv[-1]).read_text(encoding="utf-8")
            data_dir = next(
                line.removeprefix("data_dir: ") for line in answers.splitlines() if line.startswith("data_dir: ")
            )
            Path(data_dir).mkdir()
        if argv[1:] == ["skill", "install", "--tool", "cursor", "--skill", "brief"]:
            rule = cwd / ".cursor" / "rules" / "brief.md"
            rule.parent.mkdir(parents=True)
            rule.write_text("Bundled support: `ops/week-start.md`\n", encoding="utf-8")
        return _completed(argv)

    monkeypatch.setattr(runner, "_run", fake_run)

    report = runner.smoke(
        artifact,
        repo_root=_REPO_ROOT,
        source_revision="a" * 40,
        expected_version="1.0.0",
        profile="all",
    )

    assert report.ok
    assert report.profile == "all"
    install = next(argv for argv in calls if argv[-4:-2] == ["pip", "install"])
    assert install[-1] == f"{artifact.resolve()}[all]"
    criterion_ids = {criterion.criterion_id for criterion in report.criteria}
    assert {"SMOKE201", "SMOKE202", "SMOKE203", "SMOKE204", "SMOKE205", "SMOKE206"} <= criterion_ids
    assert "SMOKE107" not in criterion_ids
    assert "SMOKE110" not in criterion_ids
    assert any(argv[1:] == ["skill", "eval", "--help"] for argv in calls)
    assert not any("driver" in argv for argv in calls)


def test_uv_installer_targets_the_exact_artifact_and_created_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"exact candidate")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout
        calls.append(argv)
        return _completed(argv, returncode=1 if argv[:3] == ["/usr/bin/uv", "pip", "install"] else 0)

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda command: "/usr/bin/uv" if command == "uv" else None)

    runner.smoke(
        artifact,
        repo_root=_REPO_ROOT,
        source_revision="a" * 40,
        expected_version="1.0.0",
        profile="all",
        installer="uv",
    )

    create = next(argv for argv in calls if argv[:2] == ["/usr/bin/uv", "venv"])
    assert create[2:4] == ["--python", sys.executable]
    assert Path(create[-1]).name == "venv"
    install = next(argv for argv in calls if argv[:3] == ["/usr/bin/uv", "pip", "install"])
    assert install[3] == "--python"
    assert Path(install[4]).parent.name == "bin"
    assert Path(install[4]).parent.parent.name == "venv"
    assert install[-1] == f"{artifact.resolve()}[all]"


def test_smoke_gives_fresh_environment_creation_a_bounded_setup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh virtual environment may take longer than a single CLI command."""
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"exact candidate")
    observed_timeouts: list[int] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if "venv" in argv:
            observed_timeouts.append(timeout)
            return _completed(argv, returncode=1, stderr="environment setup failed")
        return _completed(argv)

    monkeypatch.setattr(runner, "_run", fake_run)

    report = runner.smoke(
        artifact,
        repo_root=_REPO_ROOT,
        source_revision="a" * 40,
        expected_version="1.0.0",
    )

    assert report.ok is False
    assert observed_timeouts == [runner.VENV_CREATE_TIMEOUT_SECONDS]


def test_main_emits_versioned_json_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"candidate")
    report = runner.SmokeReport(
        2,
        "b" * 40,
        artifact.name,
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "base",
        "3.11.0",
        "test-platform",
        (runner.SmokeCriterion("SMOKE001", "fail", "creation failed"),),
    )
    monkeypatch.setattr(runner, "smoke", lambda *args, **kwargs: report)

    exit_code = runner.main(["--json", str(artifact)])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["status"] == "fail"
    assert payload["criteria"][0]["criterion_id"] == "SMOKE001"
