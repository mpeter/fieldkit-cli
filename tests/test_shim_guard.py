"""Focused contracts for the R25 pre-commit shim guard."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hooks import shim_guard

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("symbol", "line"),
    [
        ("FrontmatterStalenessError", 1),
        ("GmailAuthError", 1),
        ("LLMError", 1),
        ("LLMErrorCategory", 2),
    ],
)
def test_reexport_alias_violations_detects_protected_symbols(symbol: str, line: int) -> None:
    source = "\n" * (line - 1) + f"from fieldkit.errors import {symbol} as {symbol}\n"

    result = shim_guard._reexport_alias_violations(source)

    assert result == [(line, symbol)]


@pytest.mark.parametrize(
    "source",
    [
        "from fieldkit.errors import LLMError\n",
        "try:\n    pass\nexcept LLMError as exc:\n    raise exc\n",
        "from example import OtherError as OtherError\n",
        "def broken(:\n",
    ],
)
def test_reexport_alias_violations_allows_non_resurrections(source: str) -> None:
    result = shim_guard._reexport_alias_violations(source)

    assert result == []


def test_get_modified_python_files_selects_python_and_excludes_package_aggregators() -> None:
    completed = MagicMock(returncode=0, stdout="src/fieldkit/llm/core.py\nsrc/fieldkit/sf/__init__.py\nREADME.md\n")

    with patch.object(shim_guard.subprocess, "run", return_value=completed) as run:
        result = shim_guard._get_modified_python_files()

    assert result == [Path("src/fieldkit/llm/core.py")]
    run.assert_called_once_with(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=MR"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_main_reports_modified_reexport_with_location(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    modified = tmp_path / "module.py"
    modified.write_text("from fieldkit.errors import LLMError as LLMError\n")

    with (
        patch.object(shim_guard, "_get_staged_python_files", return_value=[]),
        patch.object(shim_guard, "_get_modified_python_files", return_value=[modified]),
        patch.object(shim_guard, "_read_staged_source", side_effect=lambda path: path.read_text()),
    ):
        result = shim_guard.main()

    assert result == 1
    assert f"blocked: {modified}:1 (LLMError as LLMError)" in capsys.readouterr().err


def test_main_returns_zero_when_package_aggregator_is_excluded(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(shim_guard, "_get_staged_python_files", return_value=[]),
        patch.object(shim_guard, "_get_modified_python_files", return_value=[]),
    ):
        result = shim_guard.main()

    assert result == 0
    assert capsys.readouterr().err == ""


def test_main_allows_normal_import_in_modified_module(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    modified = tmp_path / "module.py"
    modified.write_text("from fieldkit.errors import LLMError\n")

    with (
        patch.object(shim_guard, "_get_staged_python_files", return_value=[]),
        patch.object(shim_guard, "_get_modified_python_files", return_value=[modified]),
        patch.object(shim_guard, "_read_staged_source", side_effect=lambda path: path.read_text()),
    ):
        result = shim_guard.main()

    assert result == 0
    assert capsys.readouterr().err == ""


def test_main_honors_modified_file_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    modified = tmp_path / "module.py"
    modified.write_text("# shim-guard: ignore\nfrom fieldkit.errors import LLMError as LLMError\n")

    with (
        patch.object(shim_guard, "_get_staged_python_files", return_value=[]),
        patch.object(shim_guard, "_get_modified_python_files", return_value=[modified]),
        patch.object(shim_guard, "_read_staged_source", side_effect=lambda path: path.read_text()),
    ):
        result = shim_guard.main()

    assert result == 0
    assert capsys.readouterr().err == ""


def test_main_reports_added_and_modified_violations_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    added = tmp_path / "old_surface.py"
    added.write_text("from fieldkit.sf import SFRecord as SFRecord\n")
    modified = tmp_path / "module.py"
    modified.write_text("from fieldkit.errors import GmailAuthError as GmailAuthError\n")

    with (
        patch.object(shim_guard, "_get_staged_python_files", return_value=[added]),
        patch.object(shim_guard, "_get_modified_python_files", return_value=[modified]),
        patch.object(shim_guard, "_read_staged_source", side_effect=lambda path: path.read_text()),
    ):
        result = shim_guard.main()

    error = capsys.readouterr().err
    assert result == 1
    assert f"blocked: {added}" in error
    assert f"blocked: {modified}:1 (GmailAuthError as GmailAuthError)" in error


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30)


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture User")
    _git(repo, "config", "user.email", "fixture@example.com")
    module = repo / "module.py"
    module.write_text("VALUE = 1\n" + "# retained context\n" * 10)
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "test: seed fixture")
    return repo


def test_main_reads_prohibited_alias_from_index_when_worktree_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _committed_repo(tmp_path)
    module = repo / "module.py"
    staged = module.read_text() + "from fieldkit.errors import LLMError as LLMError\n"
    module.write_text(staged)
    _git(repo, "add", "module.py")
    module.write_text(staged.replace("from fieldkit.errors import LLMError as LLMError\n", ""))
    monkeypatch.chdir(repo)

    result = shim_guard.main()

    assert result == 1
    assert "blocked: module.py:12 (LLMError as LLMError)" in capsys.readouterr().err


def test_main_checks_renamed_module_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _committed_repo(tmp_path)
    _git(repo, "mv", "module.py", "renamed.py")
    renamed = repo / "renamed.py"
    renamed.write_text(renamed.read_text() + "from fieldkit.errors import GmailAuthError as GmailAuthError\n")
    _git(repo, "add", "renamed.py")
    monkeypatch.chdir(repo)

    result = shim_guard.main()

    assert result == 1
    assert "blocked: renamed.py:12 (GmailAuthError as GmailAuthError)" in capsys.readouterr().err
