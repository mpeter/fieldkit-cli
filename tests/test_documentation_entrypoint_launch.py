"""Launch the documentation verifier without an inherited Python import path."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import documentation_commands, quality_plan

pytestmark = pytest.mark.integration
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCH_TIMEOUT_SECONDS = 30


def _environment(tmp_path: Path) -> dict[str, str]:
    """Provide tool discovery and isolated home/cache without user credentials."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "UV_NO_SYNC": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
    }


@pytest.mark.parametrize("caller", ["make", "quality-plan", "ci"])
def test_documentation_callers_launch_without_pythonpath(tmp_path: Path, caller: str) -> None:
    """Execute each real verifier command through argument parsing."""
    environment = _environment(tmp_path)
    if caller == "make":
        recipe = subprocess.run(
            ["make", "--no-print-directory", "--dry-run", "docs-examples"],
            cwd=_REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_LAUNCH_TIMEOUT_SECONDS,
        )
        assert recipe.returncode == 0, recipe.stderr
        command = shlex.split(recipe.stdout.strip())
    elif caller == "quality-plan":
        plan = quality_plan.build_plan("full", repo=_REPO_ROOT)
        assert plan.tier == "full"
        stage = next(stage for stage in plan.stages if stage.label == "docs-site")
        command = list(stage.commands[1].argv)
    else:
        workflow = (_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        command = shlex.split(
            next(line.strip() for line in workflow.splitlines() if "check_documentation_examples" in line)
        )
    result = subprocess.run(
        [*command, "--help"],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr
    assert "--require-complete" in result.stdout
    assert "--repo-root" in result.stdout
    assert result.stderr == ""


def test_module_documentation_entrypoint_rejects_missing_candidate(tmp_path: Path) -> None:
    """Reach real candidate validation and fail before any integration runs."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.check_documentation_examples",
            "--repo-root",
            str(tmp_path / "missing-candidate"),
            "--report",
            "-",
        ],
        cwd=_REPO_ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )

    assert result.returncode == 2, result.stderr
    assert "Documentation examples: ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_skill_semantic_owner_launches_without_pythonpath(tmp_path: Path) -> None:
    """Execute the exact fixed owner argv with no inherited import path."""
    command = documentation_commands.DOCUMENT_COMMANDS["skill_semantic_contract"][0]
    result = subprocess.run(
        [*command, "--help"],
        cwd=_REPO_ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr
    assert "--repo-root" in result.stdout
    assert "--json" in result.stdout
    assert result.stderr == ""


def test_skill_semantic_owner_rejects_missing_candidate(tmp_path: Path) -> None:
    """Reach contract validation after the complete real import chain loads."""
    command = documentation_commands.DOCUMENT_COMMANDS["skill_semantic_contract"][0]
    result = subprocess.run(
        [*command, "--repo-root", str(tmp_path / "missing-candidate")],
        cwd=_REPO_ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )

    assert result.returncode == 2, result.stderr
    assert "Skill documentation semantics: ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
