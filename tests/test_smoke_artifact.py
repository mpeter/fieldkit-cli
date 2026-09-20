"""Contracts for installed-artifact smoke evidence."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def _completed(
    argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_smoke_records_exact_digest_environment_and_all_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"exact candidate")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd, env))
        if argv[-1] == "--version":
            return _completed(argv, stdout="fieldkit 1.0.0\n")
        if argv[-2:] == ["web", "--help"]:
            return _completed(argv, returncode=3, stderr="requires the optional profile")
        if argv[-2:] == ["commands", "--json"]:
            return _completed(
                argv,
                stdout=(
                    '[{"full_name": "auth google"}, {"full_name": "meeting link"}, '
                    '{"full_name": "driver run"}, {"full_name": "web serve"}]'
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

    report = runner.smoke(
        artifact,
        repo_root=_REPO_ROOT,
        source_revision="a" * 40,
        expected_version="1.0.0",
    )

    assert report.ok
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
    }
    assert calls
    assert all(call_cwd != _REPO_ROOT for _, call_cwd, _ in calls)
    assert all(env["PYTHONSAFEPATH"] == "1" for _, _, env in calls)
    assert all(env["PYTHONPATH"] == "" or Path(env["PYTHONPATH"]).name == "network-guard" for _, _, env in calls)
    assert all(Path(env["TMPDIR"]).name == "tmp" for _, _, env in calls)
    assert any(Path(argv[-2]).name == "fieldkit" and argv[-1] == "doctor" for argv, _, _ in calls)
    assert any(argv[-3:] == ["tool", "install", str(artifact.resolve())] for argv, _, _ in calls)
    assert any(argv[-3:] == ["init", "--minimal", "./fieldkit-workspace"] for argv, _, _ in calls)
    assert any(argv[-5:] == ["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"] for argv, _, _ in calls)
    answers_calls = [argv for argv, _, _ in calls if argv[-2:] and "--answers" in argv]
    assert len(answers_calls) == 1
    assert Path(answers_calls[0][-1]).name == "init-answers.yaml"
    offline_calls = [
        env
        for argv, _, env in calls
        if argv[-3:-1] == ["init", "--minimal"] and Path(argv[-1]).name == "offline-workspace"
    ]
    assert offline_calls and Path(offline_calls[0]["PYTHONPATH"]).name == "network-guard"
    uninstall_index = next(
        index for index, (argv, _, _) in enumerate(calls) if argv[-4:] == ["pip", "uninstall", "--yes", "fieldkit-cli"]
    )
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
        del env, timeout
        calls.append(argv)
        if argv[-1] == "--version":
            return _completed(argv, stdout="fieldkit 1.0.0\n")
        if argv[-2:] == ["commands", "--json"]:
            return _completed(
                argv,
                stdout=(
                    '[{"full_name":"auth google"},{"full_name":"meeting link"},'
                    '{"full_name":"driver run"},{"full_name":"web serve"}]'
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
