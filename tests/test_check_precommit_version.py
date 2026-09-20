"""Tests for scripts/check_precommit_version.py.

``scripts/`` is added to sys.path by conftest.py, so the module can be
imported directly as ``check_precommit_version``.
"""

import subprocess
from pathlib import Path

import check_precommit_version
import pytest


def _config(tmp_path: Path, contents: str) -> Path:
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(contents, encoding="utf-8")
    return config


# ---------------------------------------------------------------------------
# _declared_floor — parsed via yaml.safe_load, not line matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_declared_floor_is_parsed_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version: '4.6.1'\n"))
    assert check_precommit_version._declared_floor() == (4, 6, 1)


@pytest.mark.unit
def test_declared_floor_survives_reformatting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A yaml.safe_load parse tolerates spacing/quoting changes a startswith match would not."""
    monkeypatch.setattr(
        check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version   :    4.6.1\n")
    )
    assert check_precommit_version._declared_floor() == (4, 6, 1)


@pytest.mark.unit
def test_declared_floor_missing_key_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "repos: []\n"))
    assert check_precommit_version._declared_floor() is None


@pytest.mark.unit
def test_declared_floor_missing_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", tmp_path / "does-not-exist.yaml")
    assert check_precommit_version._declared_floor() is None


# ---------------------------------------------------------------------------
# main() — version comparison is tuple-based, not string-based
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_below_floor_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version: '4.6.1'\n"))
    monkeypatch.setattr(check_precommit_version, "_installed_version", lambda: (4, 5, 0))

    exit_code = check_precommit_version.main()

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "4.5.0" in out
    assert "4.6.1" in out


@pytest.mark.unit
def test_tuple_comparison_not_string_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Version "4.10.0" must beat "4.6.1" — a string compare would get this backwards."""
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version: '4.6.1'\n"))
    monkeypatch.setattr(check_precommit_version, "_installed_version", lambda: (4, 10, 0))

    exit_code = check_precommit_version.main()

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.unit
def test_at_floor_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version: '4.6.1'\n"))
    monkeypatch.setattr(check_precommit_version, "_installed_version", lambda: (4, 6, 1))
    assert check_precommit_version.main() == 0


@pytest.mark.unit
def test_no_floor_declared_passes_without_checking_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "repos: []\n"))

    def _fail() -> tuple[int, int, int] | None:
        raise AssertionError("_installed_version should not be called when no floor is declared")

    monkeypatch.setattr(check_precommit_version, "_installed_version", _fail)
    assert check_precommit_version.main() == 0


@pytest.mark.unit
def test_missing_pre_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_precommit_version, "CONFIG", _config(tmp_path, "minimum_pre_commit_version: '4.6.1'\n"))
    monkeypatch.setattr(check_precommit_version, "_installed_version", lambda: None)

    exit_code = check_precommit_version.main()

    assert exit_code == 1
    assert "not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _installed_version — resolves the same python the wrapper will invoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_installed_version_uses_path_when_no_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_precommit_version, "_wrapper_python", lambda: None)

    seen_cmd: list[str] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="pre-commit 4.6.1\n", stderr="")

    monkeypatch.setattr(check_precommit_version.subprocess, "run", _fake_run)

    assert check_precommit_version._installed_version() == (4, 6, 1)
    assert seen_cmd == ["pre-commit", "--version"]


@pytest.mark.unit
def test_installed_version_prefers_wrapper_baked_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper baked to a stale INSTALL_PYTHON must be checked directly, not PATH.

    Otherwise `pip install --user --upgrade pre-commit` looks like it fixed
    things on PATH while the wrapper silently keeps invoking the old version.
    """
    baked_python = tmp_path / "baked-python"
    baked_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_precommit_version, "_wrapper_python", lambda: baked_python)

    seen_cmd: list[str] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="pre-commit 4.5.0\n", stderr="")

    monkeypatch.setattr(check_precommit_version.subprocess, "run", _fake_run)

    assert check_precommit_version._installed_version() == (4, 5, 0)
    assert seen_cmd == [str(baked_python), "-mpre_commit", "--version"]


@pytest.mark.unit
def test_wrapper_python_returns_none_without_git_common_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a git repository")

    monkeypatch.setattr(check_precommit_version.subprocess, "run", _fake_run)
    assert check_precommit_version._wrapper_python() is None


@pytest.mark.unit
def test_wrapper_python_returns_none_when_baked_path_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pruned uv cache path means the wrapper itself falls back to PATH."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "pre-commit").write_text(
        "INSTALL_PYTHON=/nonexistent/cache/path/python\n",
        encoding="utf-8",
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=str(tmp_path) + "\n", stderr="")

    monkeypatch.setattr(check_precommit_version.subprocess, "run", _fake_run)
    assert check_precommit_version._wrapper_python() is None
