"""Tests for scripts/check_install_health.py."""

import subprocess
from pathlib import Path

import check_install_health
import pytest

pytestmark = pytest.mark.unit


def _launcher(tmp_path: Path, runtime: Path) -> Path:
    launcher = tmp_path / "fieldkit"
    launcher.write_text(f"#!{runtime}\n", encoding="utf-8")
    return launcher


def test_missing_global_launcher_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_install_health.shutil, "which", lambda _: None)

    assert check_install_health.main() == 1
    assert "make install" in capsys.readouterr().err


def test_unreadable_global_launcher_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = tmp_path / "fieldkit"
    launcher.write_text("not a launcher\n", encoding="utf-8")
    monkeypatch.setattr(check_install_health.shutil, "which", lambda _: str(launcher))

    assert check_install_health.main() == 1


@pytest.mark.parametrize("missing_module", ["cryptography", "secretstorage"])
def test_missing_chrome_auth_requirement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing_module: str
) -> None:
    runtime = tmp_path / "python"
    launcher = _launcher(tmp_path, runtime)
    monkeypatch.setattr(check_install_health.shutil, "which", lambda _: str(launcher))
    monkeypatch.setattr(
        check_install_health.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="", stderr=f"No module named {missing_module}"
        ),
    )

    assert check_install_health.main() == 1
    assert "lacks chrome-auth requirements" in capsys.readouterr().err


def test_chrome_auth_in_global_runtime_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "python"
    launcher = _launcher(tmp_path, runtime)
    seen: list[str] = []
    monkeypatch.setattr(check_install_health.shutil, "which", lambda _: str(launcher))

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(check_install_health.subprocess, "run", _run)

    assert check_install_health.main() == 0
    assert seen == [
        str(runtime),
        "-c",
        "import cryptography; import secretstorage; from fieldkit.shadowbot.auth import _HAS_CHROME_AUTH; assert _HAS_CHROME_AUTH",
    ]
    assert "includes chrome-auth requirements" in capsys.readouterr().out
