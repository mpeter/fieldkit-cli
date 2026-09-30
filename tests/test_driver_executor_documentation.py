"""Structural and invocation checks for the packaged executor instructions."""

from importlib.resources import files
from pathlib import Path

import pytest

from fieldkit.cli_registry import walk_cli
from fieldkit.driver.opencode import _build_opencode_command

pytestmark = pytest.mark.unit


def test_packaged_driver_is_reachable_through_the_command_registry() -> None:
    result = {node.full_name for node in walk_cli()}

    assert {"driver", "driver run", "driver status", "driver retry status"} <= result


def test_executor_instructions_are_the_exact_portable_invocation(tmp_path: Path) -> None:
    instructions = files("fieldkit._data").joinpath("driver-executor.md").read_text(encoding="utf-8")
    prompt = tmp_path / "work-order.md"

    result = _build_opencode_command("opencode", tmp_path, prompt, None)

    assert result == [
        "opencode",
        "run",
        "--pure",
        "--auto",
        "--agent",
        "build",
        "--dir",
        str(tmp_path),
        "--file",
        str(prompt),
        "--",
        f"{instructions}\n\nExecute the attached prompt.",
    ]
    assert "independent verifier runs the frozen" in instructions
    assert "verifier-private" in instructions
    assert "claim the authoritative verification passed" in instructions
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".opencode").exists()


def test_installed_artifact_probe_requires_executor_asset() -> None:
    source = (Path(__file__).parents[1] / "scripts/smoke_artifact.py").read_text(encoding="utf-8")

    assert "'_data/driver-executor.md'" in source
