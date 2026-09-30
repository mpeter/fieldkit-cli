"""Fail-closed contracts for installed local watcher and environment examples."""

import subprocess
from pathlib import Path

import pytest

from scripts import smoke_artifact as smoke

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure", [None, "exit", "output", "status-json", "watch-write", "brief-write", "identity"])
def test_local_examples_require_outputs_and_respect_write_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, str]]] = []
    environment = {"PYTHONPATH": "network-guard", "FIELDKIT_NO_LLM": "1"}

    def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path and timeout == smoke.COMMAND_TIMEOUT_SECONDS
        calls.append((argv, env))
        command = argv[1:]
        if command[0] == "-c":
            assert env["FIELDKIT_USER_EMAIL"] == "user@example.com"
            return subprocess.CompletedProcess(argv, 1 if failure == "identity" else 0, "", "")
        if command == ["watch", "run", "pursuit-stalls", "--dry-run"]:
            if failure == "watch-write":
                (workspace / "watchers").mkdir()
            output = "[DRY-RUN] pursuit-stalls: 1 pursuit(s) scanned, 0 stall alert(s) would fire\n"
        elif command == ["watch", "status"]:
            output = "No watcher runs recorded yet. Run 'fieldkit watch run --all' to start.\n"
        elif command == ["watch", "status", "--json"]:
            output = "{}" if failure == "status-json" else '{"items": [], "count": 0, "filters": {}}'
        elif command == ["watch", "logs", "--list"]:
            output = "Recent log files (1):\n pursuit-stalls-example.log\n"
        elif command == ["watch", "logs", "pursuit-stalls", "--tail", "100"]:
            output = "Run complete: checked=1 stalled=0 dry_run=True\n"
        elif command == ["brief", "generate", "--pipeline-only", "--dry-run"]:
            assert env["FIELDKIT_NO_LLM"] == "1"
            if failure == "brief-write":
                (workspace / "briefs").mkdir()
            output = "[LLM STUB] FIELDKIT_NO_LLM=1 is set — no API call was made.\n"
        else:
            assert command == ["doctor"]
            assert env["FIELDKIT_USER_EMAIL"] == "user@example.com"
            output = "fieldkit doctor\n"
        return subprocess.CompletedProcess(
            argv, 3 if failure == "exit" else 0, "" if failure == "output" else output, ""
        )

    monkeypatch.setattr(smoke, "_run", run)
    criteria = smoke._local_examples(Path("fieldkit"), workspace=workspace, cwd=tmp_path, env=environment)

    expected_failures = {
        None: set(),
        "exit": {f"SMOKE{number}" for number in range(133, 140)},
        "output": {f"SMOKE{number}" for number in range(133, 140)},
        "status-json": {"SMOKE135"},
        "watch-write": {"SMOKE133"},
        "brief-write": {"SMOKE138"},
        "identity": {"SMOKE140"},
    }
    assert len(criteria) == 8
    assert {item.criterion_id for item in criteria if item.status != "pass"} == expected_failures[failure]
    assert [item.criterion_id for item in criteria] == [f"SMOKE{number}" for number in range(133, 141)]
    assert [argv for argv, _ in calls] == [
        ["fieldkit", "watch", "run", "pursuit-stalls", "--dry-run"],
        ["fieldkit", "watch", "status"],
        ["fieldkit", "watch", "status", "--json"],
        ["fieldkit", "watch", "logs", "--list"],
        ["fieldkit", "watch", "logs", "pursuit-stalls", "--tail", "100"],
        ["fieldkit", "brief", "generate", "--pipeline-only", "--dry-run"],
        ["fieldkit", "doctor"],
        [
            "python",
            "-c",
            "from fieldkit.config import get_user_email_from_env; assert get_user_email_from_env() == 'user@example.com'",
        ],
    ]
    assert all(env["PYTHONPATH"] == "network-guard" for _, env in calls)
