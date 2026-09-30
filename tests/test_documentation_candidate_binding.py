"""Candidate identity must resist local Git shortcuts and redirection."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import documentation_candidate_execution as execution
from scripts.documentation_candidate_execution import candidate_binding

pytestmark = pytest.mark.unit


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=repository, check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    repository = tmp_path / "candidate"
    repository.mkdir()
    _git(repository, "init", "-q")
    (repository / "tracked.md").write_text("candidate\n", encoding="utf-8")
    (repository / "docs").mkdir()
    (repository / "docs/documentation-contract.json").write_text("{}\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate")
    return repository


def test_binding_rejects_hidden_worktree_changes(candidate: Path) -> None:
    _git(candidate, "update-index", "--assume-unchanged", "tracked.md")
    (candidate / "tracked.md").write_text("hidden change\n", encoding="utf-8")

    binding = candidate_binding(candidate, [])

    assert binding["clean"] is False
    assert binding["evidence_kind"] == "diagnostic"


def test_binding_ignores_git_repository_redirection(candidate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_revision = _git(candidate, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(candidate / "missing.git"))

    binding = candidate_binding(candidate, [])

    assert binding["source_revision"] == expected_revision
    assert binding["clean"] is True


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_tree_identity_rejects_oversized_process_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stdout.write('a' * 40 + '\\n'); sys.stdout.flush()\n"
        f"sys.{stream}.write(' ' * (64 * 1024 + 1)); sys.{stream}.flush()\n",
        encoding="utf-8",
    )
    git.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ValueError, match=r"^candidate tree identity is unavailable$"):
        execution._tree_identity(tmp_path, "b" * 40)


@pytest.mark.parametrize("payload,exit_code", [(b"a" * 40, 1), (b"\xff", 0), (b"private-sentinel", 0)])
def test_tree_identity_rejects_incomplete_or_invalid_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, exit_code: int
) -> None:
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.buffer.write({payload!r})\n"
        "sys.stderr.write('private-sentinel')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    git.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ValueError, match=r"^candidate tree identity is unavailable$"):
        execution._tree_identity(tmp_path, "b" * 40)


def test_tree_identity_preserves_finite_process_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.util.bounded_process import BoundedProcessBytesResult, BoundedProcessError

    def timeout(
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        cleanup_timeout: float,
        env: dict[str, str],
    ) -> BoundedProcessBytesResult:
        assert argv == ["git", "--no-replace-objects", "rev-parse", "b" * 40 + "^{tree}"]
        assert cwd == tmp_path
        assert timeout == 10
        assert stdout_limit == stderr_limit == 64 * 1024
        assert cleanup_timeout == 5
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        raise BoundedProcessError("private-sentinel", reason="timeout")

    monkeypatch.setattr(execution, "run_bounded_process_bytes", timeout)

    with pytest.raises(ValueError, match=r"^candidate tree identity is unavailable$") as error:
        execution._tree_identity(tmp_path, "b" * 40)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b" " * (5 * 1024 * 1024 + 1), id="oversized"),
        pytest.param(b"[" * 65 + b"0" + b"]" * 65, id="deep"),
        pytest.param(b'{"private-sentinel":0,"private-sentinel":1}', id="duplicate"),
        pytest.param(b"not JSON", id="malformed"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
def test_binding_rejects_invalid_committed_contract(candidate: Path, content: bytes) -> None:
    (candidate / "docs/documentation-contract.json").write_bytes(content)
    _git(candidate, "add", ".")
    _git(candidate, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "contract")

    binding = candidate_binding(candidate, [])

    assert binding["clean"] is False
    assert binding["evidence_kind"] == "diagnostic"
    assert binding["documentation_contract_sha256"] is None


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo"])
def test_binding_rejects_unavailable_contract(candidate: Path, kind: str) -> None:
    contract = candidate / "docs/documentation-contract.json"
    contract.unlink()
    if kind == "symlink":
        contract.symlink_to(candidate / "tracked.md")
    elif kind == "fifo":
        os.mkfifo(contract)

    binding = candidate_binding(candidate, [])

    assert binding["clean"] is False
    assert binding["documentation_contract_sha256"] is None


def test_binding_hashes_exact_contract_bytes(candidate: Path) -> None:
    content = b'{\r\n "description": "caf\\u00e9"\r\n}\r\n'
    (candidate / "docs/documentation-contract.json").write_bytes(content)
    _git(candidate, "add", ".")
    _git(candidate, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "contract")

    binding = candidate_binding(candidate, [])

    assert binding["clean"] is True
    assert binding["documentation_contract_sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'"' + b"x" * (5 * 1024 * 1024 - 2) + b'"', id="exact-byte-bound"),
        pytest.param(b"[" * 64 + b"0" + b"]" * 64, id="exact-depth-bound"),
    ],
)
def test_contract_digest_accepts_resource_boundary(tmp_path: Path, content: bytes) -> None:
    contract = tmp_path / "contract.json"
    contract.write_bytes(content)

    digest = execution._contract_sha256(contract)

    assert digest == hashlib.sha256(content).hexdigest()
