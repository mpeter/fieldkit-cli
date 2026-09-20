"""Process and boundary tests for independent done-check execution."""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.driver.done_check_executor as executor
from fieldkit.driver.done_check_executor import _git as verifier_git
from fieldkit.driver.done_check_executor import _run_check, verify_submitted_head
from fieldkit.driver.github import PullRequestIdentity
from fieldkit.errors import AuthError
from tests.test_driver_done_check_executor import (
    _git,
    _identity,
    _repository,
    _snapshot,
)

pytestmark = pytest.mark.unit


def _is_dead_or_zombie(process_state: Path) -> bool:
    """Return whether a process has exited without racing its procfs removal."""
    try:
        return process_state.read_text().split()[2] == "Z"
    except FileNotFoundError:
        return True


@pytest.fixture(autouse=True)
def _use_test_tool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_prepare_tool_environment", lambda *_args: Path(sys.prefix))


@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (PullRequestIdentity(9, "example/repo", 42, "release", "driver/issue-1242-test", "a" * 40), "base"),
        (PullRequestIdentity(9, "example/repo", 42, "main", "driver/other", "a" * 40), "head"),
    ],
)
def test_base_or_head_mismatch_fails_before_fetch(tmp_path: Path, identity: PullRequestIdentity, message: str) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=identity),
        patch("fieldkit.driver.done_check_executor._git", wraps=executor._git) as git_call,
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert message in result.reason
    assert not any(call.args[1:2] == ("fetch",) for call in git_call.call_args_list)


def test_expected_exit_two_is_an_ordinary_contract_value(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: exit-two\n      argv: [python, tests/check.py]\n      expected_exit: 2\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "check.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    _git(repo, "add", "tests/check.py")
    _git(repo, "commit", "-m", "test: add exit check")
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

    assert result.passed is True
    assert result.checks[0].exit_code == 2


def test_trusted_checker_consumes_private_producer_artifact(tmp_path: Path) -> None:
    checks = (
        "    - id: produce\n"
        "      argv: [python, tests/produce.py, '{artifacts}/result.txt']\n"
        "    - id: inspect\n"
        "      checker: scripts/done_checks/check_result.py\n"
        "      args: ['{artifacts}/result.txt']\n"
    )
    checker = (
        "from pathlib import Path\nimport sys\nraise SystemExit(0 if Path(sys.argv[1]).read_text() == 'ok' else 1)\n"
    )
    repo, work_order, _ = _repository(
        tmp_path,
        checks,
        {
            "scripts/done_checks/check_result.py": checker,
            "scripts/check_done_checkers.py": (
                Path(__file__).parent.parent / "scripts/check_done_checkers.py"
            ).read_text(),
        },
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "produce.py").write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ok')\n", encoding="utf-8"
    )
    _git(repo, "add", "tests/produce.py")
    _git(repo, "commit", "-m", "test: add producer")
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

    assert result.passed is True
    assert [check.status for check in result.checks] == ["passed", "passed"]


def test_aggregate_make_is_not_given_the_ordinary_command_timeout(tmp_path: Path) -> None:
    repo, work_order, head = _repository(
        tmp_path,
        "    - id: quality\n      argv: [make, quality]\n",
        {"Makefile": "quality:\n\t@sleep 0.2\n"},
    )
    snapshot = _snapshot(repo, work_order, tmp_path)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)),
        patch("fieldkit.driver.done_check_executor.TIMEOUT_DRIVER_CHECK", 0.05),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is True
    assert result.checks[0].duration_ms >= 150


def test_make_uses_frozen_makefile_even_when_candidate_adds_gnumakefile(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: quality\n      argv: [make, quality]\n",
        {"Makefile": "quality:\n\t@false\n"},
    )
    (repo / "GNUmakefile").write_text("quality:\n\t@true\n", encoding="utf-8")
    _git(repo, "add", "GNUmakefile")
    _git(repo, "commit", "-m", "test: add candidate make override")
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
    assert result.checks[0].status == "failed"


def test_python_check_retains_trusted_virtual_environment(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: import\n      argv: [python, tests/import_yaml.py]\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "import_yaml.py").write_text("import yaml\n", encoding="utf-8")
    _git(repo, "add", "tests/import_yaml.py")
    _git(repo, "commit", "-m", "test: import installed dependency")
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

    assert result.passed is True


def test_git_authentication_failure_remains_typed(tmp_path: Path) -> None:
    failure = subprocess.CompletedProcess([], 1, "", "fatal: Authentication failed")
    with (
        patch("fieldkit.driver.done_check_executor.subprocess.run", return_value=failure),
        pytest.raises(AuthError, match="Authentication failed"),
    ):
        verifier_git(tmp_path, "fetch", "origin", "a" * 40)


def test_make_uses_the_snapshotted_host_python_environment(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: quality\n      argv: [make, quality]\n",
        {"Makefile": "quality:\n\t@true\n"},
    )
    snapshot = _snapshot(repo, work_order, tmp_path)

    environment = executor._minimal_environment(snapshot, repo, snapshot.contract.checks[0])

    assert environment["UV_PROJECT_ENVIRONMENT"] == snapshot.python_environment
    assert "HOME" not in environment


def test_candidate_virtualenv_cannot_shadow_trusted_leaf(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: grep\n      argv: [grep, -F, hello, README.md]\n")
    malicious = repo / ".venv" / "bin" / "grep"
    malicious.parent.mkdir(parents=True)
    malicious.write_text("#!/bin/sh\ntouch MALICIOUS\nexit 0\n", encoding="utf-8")
    malicious.chmod(0o755)
    _git(repo, "add", "-f", ".venv/bin/grep")
    _git(repo, "commit", "-m", "test: add shadow tool")
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

    assert result.passed is True


def test_auth_failure_writes_local_failure_evidence_then_propagates(tmp_path: Path) -> None:
    from fieldkit.errors import AuthError

    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    evidence = tmp_path / "evidence"

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", side_effect=AuthError("login required")),
        pytest.raises(AuthError, match="login required"),
    ):
        verify_submitted_head(
            snapshot,
            repo,
            evidence_root=evidence,
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    payload = json.loads(next(evidence.glob("*.json")).read_text())
    assert payload["decision"] == "failed"
    assert payload["phase"] == "failure"


def test_artifact_cleanup_failure_denies_success(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)),
        patch("fieldkit.driver.done_check_executor.shutil.rmtree", side_effect=OSError("cleanup failed")),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is False
    assert result.cleanup_status == "artifact-remove"


def test_unexpected_verification_error_still_attempts_cleanup(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)

    with (
        patch("fieldkit.driver.done_check_executor._execute_verification", side_effect=RuntimeError("bug")),
        patch("fieldkit.driver.done_check_executor._cleanup_verifier", return_value="passed") as cleanup,
        pytest.raises(RuntimeError, match="bug"),
    ):
        verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    cleanup.assert_called_once()


def test_evidence_brackets_verifier_cleanup(tmp_path: Path) -> None:
    repo, work_order, head = _repository(tmp_path, "    - id: exists\n      argv: [test, -f, README.md]\n")
    snapshot = _snapshot(repo, work_order, tmp_path)
    events: list[str] = []
    real_write = executor.write_evidence  # pyright: ignore[reportPrivateImportUsage]
    real_rmtree = executor.shutil.rmtree  # pyright: ignore[reportPrivateImportUsage]

    def record_write(root: Path, record):
        events.append(record.phase)
        return real_write(root, record)

    def record_rmtree(path: Path, *args, **kwargs):
        if path.name.startswith("artifacts-"):
            events.append("cleanup")
        return real_rmtree(path, *args, **kwargs)

    with (
        patch("fieldkit.driver.done_check_executor.get_pr_identity", return_value=_identity(head)),
        patch("fieldkit.driver.done_check_executor.write_evidence", side_effect=record_write),
        patch("fieldkit.driver.done_check_executor.shutil.rmtree", side_effect=record_rmtree),
    ):
        result = verify_submitted_head(
            snapshot,
            repo,
            evidence_root=tmp_path / "evidence",
            verifier_root=tmp_path / "verifier",
            run_started_monotonic=time.monotonic(),
        )

    assert result.passed is True
    assert events == ["checks-complete", "cleanup", "terminal"]


def test_snapshot_cleanup_failure_is_observable(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    with patch("fieldkit.driver.done_check_executor._bounded_rmtree", return_value=False):
        removed = executor._remove_snapshot_dir(snapshot_dir)

    assert removed is False
    assert snapshot_dir.exists()


def test_timeout_terminates_process_group_descendants(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: descendants\n      argv: [python, tests/descendants.py, '{artifacts}/child.pid']\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "descendants.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/descendants.py")
    _git(repo, "commit", "-m", "test: add descendant process")
    snapshot = _snapshot(repo, work_order, tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    # Leave enough startup time for the helper to create its descendant under
    # full-suite xdist load; the five-second deadline still bounds the test.
    with patch("fieldkit.driver.done_check_executor.TIMEOUT_DRIVER_CHECK", 2.0):
        result = _run_check(
            snapshot,
            snapshot.contract.checks[0],
            repo,
            artifacts,
            deadline=time.monotonic() + 5,
            attempt_bytes=[0],
        )

    child_pid = int((artifacts / "child.pid").read_text())
    process_state = Path(f"/proc/{child_pid}/stat")
    for _ in range(20):
        if not process_state.exists() or process_state.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    assert result.status == "timeout"
    assert not process_state.exists() or process_state.read_text().split()[2] == "Z"


def test_open_descendant_pipe_is_killed_after_leader_exits(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: descendants\n      argv: [python, tests/descendants.py, '{artifacts}/child.pid']\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "descendants.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/descendants.py")
    _git(repo, "commit", "-m", "test: add pipe-holding descendant")
    snapshot = _snapshot(repo, work_order, tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with patch("fieldkit.driver.done_check_executor.TIMEOUT_DRIVER_OUTPUT_DRAIN", 0.05):
        result = _run_check(
            snapshot,
            snapshot.contract.checks[0],
            repo,
            artifacts,
            deadline=time.monotonic() + 5,
            attempt_bytes=[0],
        )

    child_pid = int((artifacts / "child.pid").read_text())
    process_state = Path(f"/proc/{child_pid}/stat")
    for _ in range(20):
        if _is_dead_or_zombie(process_state):
            break
        time.sleep(0.05)
    assert result.status == "stream-error"
    assert _is_dead_or_zombie(process_state)


def test_redirected_descendant_is_killed_after_leader_exits(tmp_path: Path) -> None:
    repo, work_order, _ = _repository(
        tmp_path,
        "    - id: descendants\n      argv: [python, tests/descendants.py, '{artifacts}/child.pid']\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "descendants.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/descendants.py")
    _git(repo, "commit", "-m", "test: add redirected descendant")
    snapshot = _snapshot(repo, work_order, tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    result = _run_check(
        snapshot,
        snapshot.contract.checks[0],
        repo,
        artifacts,
        deadline=time.monotonic() + 5,
        attempt_bytes=[0],
    )

    child_pid = int((artifacts / "child.pid").read_text())
    process_state = Path(f"/proc/{child_pid}/stat")
    for _ in range(20):
        if _is_dead_or_zombie(process_state):
            break
        time.sleep(0.05)
    assert result.status == "passed"
    assert _is_dead_or_zombie(process_state)
