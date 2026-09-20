"""Integration tests for trusted snapshots and exact-head execution."""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.driver.done_check_executor as executor
from fieldkit.driver.done_check_executor import (
    VerificationError,
    create_trusted_snapshot,
    verify_submitted_head,
)
from fieldkit.driver.github import PullRequestIdentity

pytestmark = pytest.mark.unit
_REAL_PREPARE_TOOL_ENVIRONMENT = executor._prepare_tool_environment


@pytest.fixture(autouse=True)
def _use_test_tool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_prepare_tool_environment", lambda *_args: Path(sys.prefix))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def _document(checks: str) -> str:
    return f"""---
issues: ["#1242"]
done_checks:
  version: 1
  checks:
{checks}---
# Work order
"""


def _repository(tmp_path: Path, checks: str, base_files: dict[str, str] | None = None) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "developer@example.com")
    _git(repo, "config", "user.name", "Example Developer")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "docs" / "work-orders").mkdir(parents=True)
    (repo / "docs" / "work-orders" / "work.md").write_text(_document(checks), encoding="utf-8")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    for relative, content in (base_files or {}).items():
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test: trusted base")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "switch", "-c", "driver/issue-1242-test")
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "test: candidate")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "main"))
    return repo, repo / "docs" / "work-orders" / "work.md", head


def _identity(head: str) -> PullRequestIdentity:
    return PullRequestIdentity(9, "example/repo", 42, "main", "driver/issue-1242-test", head)


def _snapshot(repo: Path, work_order: Path, tmp_path: Path):
    return create_trusted_snapshot(
        repo,
        work_order,
        repository="example/repo",
        issue_number=1242,
        attempt=1,
        branch="driver/issue-1242-test",
        snapshot_root=tmp_path / "snapshots",
    )


def test_prepare_tool_environment_is_private_and_preserves_symlink_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    snapshot_dir = tmp_path / "snapshot"
    repo.mkdir()
    snapshot_dir.mkdir()
    uv = repo / ".venv" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.write_bytes(b"uv")
    target = tmp_path / "python-target"
    target.write_bytes(b"python")
    target.chmod(0o755)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = snapshot_dir / "tool-environment"
        (environment / "bin").mkdir(parents=True)
        (environment / "bin" / "python").symlink_to(target)
        return subprocess.CompletedProcess([], 0, "", "")

    with (
        patch("fieldkit.driver.done_check_executor._git_bytes", return_value=b"trusted"),
        patch("fieldkit.driver.done_check_executor.subprocess.run", side_effect=run) as sync,
    ):
        environment = _REAL_PREPARE_TOOL_ENVIRONMENT(repo, "a" * 40, snapshot_dir)

    assert environment == snapshot_dir / "tool-environment"
    assert environment != Path(sys.prefix)
    assert sync.call_args.args[0][1:4] == ["sync", "--frozen", "--no-install-project"]
    assert sync.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(environment)
    assert target.stat().st_mode & 0o777 == 0o755
    assert environment.stat().st_mode & 0o777 == 0o555


def test_snapshot_uses_trusted_base_even_when_candidate_deletes_work_order(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    work_order.unlink()

    assert snapshot.base_sha == _git(repo, "rev-parse", "main")
    assert snapshot.contract.checks[0].id == "exists"
    assert (snapshot.snapshot_dir / "work-order.md").is_file()


def test_verify_submitted_head_runs_checks_and_writes_terminal_evidence(tmp_path: Path) -> None:
    repo, work_order, head = _repository(
        tmp_path,
        '    - id: output\n      argv: [grep, -F, hello, README.md]\n      expected_stdout: "hello\\n"\n',
    )
    snapshot = _snapshot(repo, work_order, tmp_path)

    with patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is True
    assert result.checks[0].status == "passed"
    assert result.cleanup_status == "passed"
    payload = json.loads(result.evidence_path.read_text()) if result.evidence_path else {}
    assert payload["phase"] == "terminal"
    assert payload["initial_head_sha"] == head
    assert not (tmp_path / "verifier" / f"head-{snapshot.attempt_id}").exists()


def test_authority_change_prevents_check_execution(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    (repo / "Makefile").write_text("quality:\n\t@true\n", encoding="utf-8")
    _git(repo, "add", "Makefile")
    _git(repo, "commit", "-m", "test: alter authority")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "HEAD")

    with patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert result.reason == "authority_changed"
    assert result.checks == ()


def test_check_that_mutates_candidate_tree_fails(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: mutate\n      argv: [python, tests/mutate.py]\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "mutate.py").write_text("from pathlib import Path\nPath('README.md').write_text('changed')\n")
    _git(repo, "add", "tests/mutate.py")
    _git(repo, "commit", "-m", "test: add candidate check")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "HEAD")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert "changed the submitted tree" in result.reason


def test_snapshot_rejects_work_order_outside_repository(tmp_path: Path) -> None:
    repo, _, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    outside = tmp_path / "outside.md"
    outside.write_text("irrelevant")

    with pytest.raises(VerificationError, match="outside"):
        _snapshot(repo, outside, tmp_path)


def test_snapshot_setup_failure_removes_read_only_checker_tree(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: checker\n      checker: scripts/done_checks/check.py\n",
        {
            "scripts/done_checks/check.py": "raise SystemExit(0)\n",
            "scripts/check_done_checkers.py": "raise SystemExit(0)\n",
        },
    )
    snapshots = tmp_path / "snapshots"

    with (
        patch("fieldkit.driver.done_check_executor._trusted_executables", side_effect=VerificationError("missing")),
        pytest.raises(VerificationError, match="missing"),
    ):
        create_trusted_snapshot(
            repo,
            work_order,
            repository="example/repo",
            issue_number=1242,
            attempt=1,
            branch="driver/issue-1242-test",
            snapshot_root=snapshots,
        )

    assert list(snapshots.iterdir()) == []


@pytest.mark.parametrize(
    ("source", "constant", "limit", "expected_status"),
    [
        ("import time\ntime.sleep(2)\n", "TIMEOUT_DRIVER_CHECK", 0.05, "timeout"),
        ("print('x' * 10000)\n", "DRIVER_CHECK_OUTPUT_BYTES", 100, "output-overflow"),
    ],
)
def test_bounded_process_failures_stop_verification(
    tmp_path: Path, source: str, constant: str, limit: float, expected_status: str
) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: bounded\n      argv: [python, tests/check.py]\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "check.py").write_text(source, encoding="utf-8")
    _git(repo, "add", "tests/check.py")
    _git(repo, "commit", "-m", "test: add bounded check")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "HEAD")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)),
        patch(f"fieldkit.driver.done_check_executor.{constant}", limit),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert result.checks[0].status == expected_status
    assert result.checks[0].stdout.byte_count <= 64 * 1024


def test_tampered_trusted_checker_is_rejected_before_launch(tmp_path: Path) -> None:
    repo, work_order, head = _repository(
        tmp_path,
        "    - id: checker\n      checker: scripts/done_checks/check.py\n",
        {
            "scripts/done_checks/check.py": "from pathlib import Path\nraise SystemExit(0)\n",
            "scripts/check_done_checkers.py": "raise SystemExit(0)\n",
        },
    )
    snapshot = _snapshot(repo, work_order, tmp_path)
    snapshot.checkers[0].snapshot_path.chmod(0o600)
    snapshot.checkers[0].snapshot_path.write_text("raise SystemExit(1)\n", encoding="utf-8")

    with patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert "trusted checker hash changed" in result.reason


def test_exhausted_shared_budget_prevents_setup(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with patch("fieldkit.driver.done_check_executor.get_pr_identity") as lookup:
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic() - executor.TIMEOUT_DRIVER_RUN,
        )

    assert result.passed is False
    assert result.reason == "verification budget exhausted before setup"
    lookup.assert_not_called()


def test_cleanup_that_exhausts_run_budget_denies_success(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    real_monotonic = time.monotonic
    started = real_monotonic()
    exhausted = False

    def clock() -> float:
        return started + executor.TIMEOUT_DRIVER_RUN if exhausted else real_monotonic()

    def cleanup(*args: object) -> str:
        nonlocal exhausted
        exhausted = True
        return "passed"

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)) as lookup,
        patch("fieldkit.driver.done_check_executor._cleanup_verifier", side_effect=cleanup),
        patch("fieldkit.driver.done_check_executor.time.monotonic", side_effect=clock),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=started,
        )

    assert result.passed is False
    assert result.reason == "driver run budget exhausted during verifier cleanup"
    assert lookup.call_count == 1


def test_final_identity_lookup_crossing_run_deadline_denies_success(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    real_monotonic = time.monotonic
    started = real_monotonic()
    exhausted = False
    lookups = 0

    def clock() -> float:
        return started + executor.TIMEOUT_DRIVER_RUN if exhausted else real_monotonic()

    def lookup(*args: object, **kwargs: object) -> PullRequestIdentity:
        nonlocal exhausted, lookups
        lookups += 1
        if lookups == 2:
            exhausted = True
        return _identity(head)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", side_effect=lookup),
        patch("fieldkit.driver.done_check_executor.time.monotonic", side_effect=clock),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=started,
        )

    assert result.passed is False
    assert result.reason == "driver run budget exhausted during final identity check"
    assert lookups == 2


def test_terminal_evidence_crossing_run_deadline_rewrites_failure(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    real_monotonic = time.monotonic
    real_write = executor.write_evidence
    started = real_monotonic()
    exhausted = False

    def clock() -> float:
        return started + executor.TIMEOUT_DRIVER_RUN if exhausted else real_monotonic()

    def write(root: Path, record):
        nonlocal exhausted
        path = real_write(root, record)
        if record.phase == "terminal":
            exhausted = True
        return path

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)),
        patch("fieldkit.driver.done_check_executor.write_evidence", side_effect=write),
        patch("fieldkit.driver.done_check_executor.time.monotonic", side_effect=clock),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=started,
        )

    payload = json.loads(result.evidence_path.read_text()) if result.evidence_path else {}
    assert result.passed is False
    assert result.reason == "driver run budget exhausted during terminal evidence"
    assert payload["decision"] == "failed"


def test_final_identity_movement_fails_after_cleanup(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    moved = PullRequestIdentity(9, "example/repo", 42, "main", "driver/issue-1242-test", "f" * 40)

    with patch("fieldkit.driver.done_check_executor.get_pr_identity", side_effect=[_identity(head), moved]):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert result.reason == "pull request identity moved during verification"
