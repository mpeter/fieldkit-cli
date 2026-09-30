"""Public-tree assurance must describe the exact clean candidate under test."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import check_public_tree_safety, public_tree_scan

pytestmark = pytest.mark.unit
_GIT_TIMEOUT_SECONDS = 10


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("change", ["staged", "unstaged", "untracked"])
def test_dirty_candidate_rejected_before_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """A clean old commit cannot stand in for pending candidate bytes."""
    _git(tmp_path, "init", "-q")
    source = tmp_path / "README.md"
    source.write_text("initial\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(
        tmp_path,
        "-c",
        "user.name=Example Contributor",
        "-c",
        "user.email=contributor@example.com",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )
    if change == "untracked":
        source = tmp_path / "new.txt"
    source.write_text("unverified\n", encoding="utf-8")
    if change == "staged":
        _git(tmp_path, "add", "README.md")
    export = Mock(side_effect=AssertionError("dirty candidates must not be exported"))
    monkeypatch.setattr(check_public_tree_safety.export_public_tree, "export_tree", export)

    with pytest.raises(ValueError, match="clean worktree"):
        check_public_tree_safety.check(tmp_path)

    export.assert_not_called()


@pytest.mark.parametrize("change", ["none", "dirty", "head", "report"])
def test_scan_revalidates_candidate_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A scan is attributable only while its candidate remains clean and unchanged."""
    revision = "a" * 40
    clean = Mock(side_effect=[None, ValueError("clean worktree required") if change == "dirty" else None])
    head = Mock(side_effect=[revision, "b" * 40 if change == "head" else revision])
    report = Mock(source_commit="b" * 40 if change == "report" else revision)
    export = Mock()
    scan = Mock(return_value=report)
    monkeypatch.setattr(check_public_tree_safety.git_worktree, "require_clean_worktree", clean)
    monkeypatch.setattr(check_public_tree_safety.git_worktree, "head_revision", head)
    monkeypatch.setattr(check_public_tree_safety.export_public_tree, "export_tree", export)
    monkeypatch.setattr(check_public_tree_safety.public_tree_scan, "scan_public_tree", scan)

    if change == "none":
        result = check_public_tree_safety.check(tmp_path)
        assert result is report
    else:
        with pytest.raises(ValueError, match=r"clean worktree|revision changed"):
            check_public_tree_safety.check(tmp_path)

    assert clean.call_count == 2
    assert export.call_args.args[1] == revision
    scan.assert_called_once()


def test_dirty_cli_returns_nonpassing_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_public_tree_safety,
        "check",
        Mock(side_effect=ValueError("candidate verification requires a clean worktree")),
    )

    result = check_public_tree_safety.main([])

    assert result == 2
    output = capsys.readouterr()
    assert "clean worktree" in output.err
    assert "PASS" not in output.out


@pytest.mark.parametrize("passing", [True, False])
def test_cli_reports_exact_revision_or_safe_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], passing: bool
) -> None:
    revision = "a" * 40
    report = Mock(
        content_ok=passing,
        source_commit=revision,
        scanned_entries=2,
        findings=(public_tree_scan.Finding("PII001", "personal_email", "README.md", 7),),
    )
    monkeypatch.setattr(check_public_tree_safety, "check", Mock(return_value=report))

    result = check_public_tree_safety.main([])

    assert result == (0 if passing else 1)
    output = capsys.readouterr()
    if passing:
        assert f"commit {revision}" in output.out
        assert not output.err
    else:
        assert "PII001: README.md:7" in output.err
        assert "PASS" not in output.out + output.err
        assert not output.out
