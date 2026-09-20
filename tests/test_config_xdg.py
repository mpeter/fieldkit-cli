"""XDG configuration isolation for public and disposable fieldkit workflows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT_SECONDS = 30


def _run_python(code: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )


def test_config_and_cookie_paths_follow_absolute_xdg_root(tmp_path: Path) -> None:
    xdg_root = tmp_path / "xdg-config"
    env = {**os.environ, "XDG_CONFIG_HOME": str(xdg_root)}

    result = _run_python(
        "import json; from fieldkit.config._integrations import get_cookie_file; from fieldkit.config._loader import CONFIG_PATH; "
        "print(json.dumps([str(CONFIG_PATH), str(get_cookie_file())]))",
        env=env,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == [
        str(xdg_root / "fieldkit" / "config.yaml"),
        str(xdg_root / "fieldkit" / "sf-cookies.json"),
    ]


def test_relative_xdg_root_falls_back_to_home_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "relative-config"}

    result = _run_python(
        "from fieldkit.config._loader import CONFIG_PATH; print(CONFIG_PATH)",
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == str(home / ".config" / "fieldkit" / "config.yaml")


@pytest.mark.parametrize("xdg_value", ["~/trial-config", " ", ""])
def test_non_absolute_xdg_roots_fall_back_to_home_config(tmp_path: Path, xdg_value: str) -> None:
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": xdg_value}

    result = _run_python(
        "from fieldkit.config._loader import CONFIG_PATH; print(CONFIG_PATH)",
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == str(home / ".config" / "fieldkit" / "config.yaml")


def test_minimal_init_and_doctor_ignore_home_config_when_xdg_is_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg_root = tmp_path / "xdg-config"
    workspace = tmp_path / "workspace"
    operator_config = home / ".config" / "fieldkit" / "config.yaml"
    operator_config.parent.mkdir(parents=True)
    private_skill = tmp_path / "operator-checkout" / ".agents" / "skills" / "private-only" / "SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("---\nname: private-only\ndescription: Private fixture\n---\n", encoding="utf-8")
    operator_config.write_text(
        f"fieldkit_home: /should/not/be/read\nfieldkit_root: {private_skill.parents[2]}\n"
        "sf_org_url: https://example.invalid\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg_root)}

    init_result = subprocess.run(
        [sys.executable, "-m", "fieldkit", "init", "--minimal", str(workspace)],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )
    doctor_result = subprocess.run(
        [sys.executable, "-m", "fieldkit", "doctor"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )
    skill_result = subprocess.run(
        [sys.executable, "-m", "fieldkit", "skill", "list"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )

    assert init_result.returncode == 0
    assert (xdg_root / "fieldkit" / "config.yaml").is_file()
    assert "https://example.invalid" in operator_config.read_text(encoding="utf-8")
    assert doctor_result.returncode == 0
    assert "HTTP Request" not in doctor_result.stderr
    assert "UNHEALTHY" not in doctor_result.stdout
    assert skill_result.returncode == 0
    assert "private-only" not in skill_result.stdout
