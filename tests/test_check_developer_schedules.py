"""Contract tests for the OpenChamber developer-schedule policy check."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path("scripts/check_developer_schedules.py")


def _task(
    name: str, *, enabled: bool = False, provider: str = "openai", model: str = "gpt-5.6-terra"
) -> dict[str, object]:
    return {
        "name": name,
        "enabled": enabled,
        "execution": {
            "providerID": provider,
            "modelID": model,
            "prompt": f"fieldkit driver admit --job {name} --json; run the bounded workflow; fieldkit driver admit --job {name} --release --json",
        },
    }


def _run(tmp_path: Path, tasks: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "openchamber.json"
    config.write_text(json.dumps({"scheduledTasks": tasks}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_schedule_policy_admits_one_gated_openai_job(tmp_path: Path) -> None:
    result = _run(tmp_path, [_task("driver", enabled=True), _task("proctor")])

    assert result.returncode == 0
    assert "PASS" in result.stdout


def test_schedule_policy_admits_a_zero_llm_preflight_gated_job(tmp_path: Path) -> None:
    task = _task("driver", enabled=True)
    task["execution"]["prompt"] = (
        "A zero-LLM scheduler preflight already acquired the lease; run the bounded workflow; "
        "fieldkit driver admit --job driver --release --json"
    )

    result = _run(tmp_path, [task])

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([_task("driver", enabled=True), _task("proctor", enabled=True)], "at most one"),
        ([_task("driver", provider="vertex_ai")], "provider"),
        ([_task("driver", model="claude-sonnet-5")], "model"),
        ([_task("nightly-driver")], "not approved"),
        ([{"name": "driver", "execution": _task("driver")["execution"]}], "enabled must be explicitly"),
        ([], "must not be empty"),
        (
            [
                {
                    "name": "driver",
                    "enabled": True,
                    "execution": {"providerID": "openai", "modelID": "gpt-5.6-terra", "prompt": "run driver"},
                }
            ],
            "admission",
        ),
    ],
)
def test_schedule_policy_rejects_unsafe_configuration(
    tmp_path: Path, tasks: list[dict[str, object]], message: str
) -> None:
    result = _run(tmp_path, tasks)

    assert result.returncode == 1
    assert message in result.stderr
