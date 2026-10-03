"""Contracts for the release-readiness evidence entry point."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_check

pytestmark = pytest.mark.unit


def _candidate() -> SimpleNamespace:
    return SimpleNamespace()


def _policy() -> SimpleNamespace:
    return SimpleNamespace(candidate=SimpleNamespace(repository="example/fieldkit-cli", planned_tag="v1.0.0"))


def _main(tmp_path: Path, revision: str) -> int:
    return release_check.main(
        [
            "--repo",
            str(tmp_path),
            "--revision",
            revision,
            "--output-dir",
            str(tmp_path / "candidate"),
            "--output",
            str(tmp_path / "release-check.json"),
        ]
    )


def test_matching_candidate_and_governance_pass_and_return_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(release_check.check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    observed: list[Path] = []
    monkeypatch.setattr(release_check, "validate_governance", lambda _policy_path, report: observed.append(report))

    status = _main(tmp_path, revision)

    assert status == 0
    report = json.loads((tmp_path / "release-check.json").read_text(encoding="utf-8"))
    assert report["status"] == "pass"
    assert [criterion["id"] for criterion in report["criteria"]] == [
        "source-revision",
        "public-candidate",
        "release-governance",
    ]
    assert observed == [tmp_path / "candidate" / "report.json"]


def test_governance_mismatch_writes_failure_and_returns_two(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    revision = "a" * 40
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(release_check.check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())

    def mismatch(*_args: object) -> None:
        raise ValueError("candidate report planned_tag does not match policy")

    monkeypatch.setattr(release_check, "validate_governance", mismatch)

    status = _main(tmp_path, revision)

    assert status == 2
    report = json.loads((tmp_path / "release-check.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "planned_tag does not match policy" in report["criteria"][0]["evidence"]


def test_stale_revision_writes_failure_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: "b" * 40)
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                "a" * 40,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["criteria"][0]["id"] == "source-revision"


def test_dirty_worktree_writes_failure_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        release_check, "_require_clean_worktree", lambda _repo: (_ for _ in ()).throw(ValueError("clean worktree"))
    )
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                "a" * 40,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    assert "clean worktree" in json.loads(output.read_text(encoding="utf-8"))["criteria"][0]["evidence"]


def test_direct_script_invocation_resolves_its_sibling_modules(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "release_check.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(Path(__file__).parents[1]),
            "--revision",
            "0" * 40,
            "--output-dir",
            str(tmp_path / "candidate"),
            "--output",
            str(tmp_path / "result.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Release check: FAILED" in result.stdout
