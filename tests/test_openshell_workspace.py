"""Safety contracts for the opt-in workspace runner."""

import json
import signal
import subprocess
from pathlib import Path

import pytest

from scripts import openshell_workspace as workspace

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("baseline\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": "example-dev",
                "image": "example/worker:1",
                "policy": "policy.yaml",
                "snapshot": "selected-files",
                "inputs": ["source.py"],
                "environment": {"MODEL": "example-model"},
                "provider_required": False,
                "result_path": "/sandbox/output",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    "name",
    ["../outside", "/outside", ".git/config", ".env", "creds/credentials.json", "private.pem", ".codex/auth.json"],
)
def test_rejects_unsafe_inputs(project: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"relative|noncredential"):
        workspace.local_path(project, name)


def test_rejects_symlink(project: Path) -> None:
    (project / "link.py").symlink_to(project / "source.py")
    with pytest.raises(ValueError, match="symlink"):
        workspace.local_path(project, "link.py")


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"extra": 1},
        {"inputs": "source.py"},
        {"provider_required": 1},
        {"environment": {"API_KEY": "hidden"}},
        {"snapshot": "dirty"},
    ],
)
def test_manifest_validation(project: Path, change: dict[str, object]) -> None:
    path = project / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(change)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"manifest|inputs|provider|environment|unsupported"):
        workspace.load_manifest(project, "manifest.json")


def test_selected_snapshot_copies_only_explicit_files(project: Path, tmp_path: Path) -> None:
    (project / "unselected.py").write_text("private", encoding="utf-8")
    manifest = workspace.load_manifest(project, "manifest.json")
    destination = tmp_path / "baseline"
    assert workspace.snapshot(project, manifest, manifest.inputs, destination) is None
    assert sorted(path.name for path in destination.iterdir()) == ["source.py"]
    assert (destination / "source.py").read_text(encoding="utf-8") == "baseline\n"


def test_git_snapshot_ignores_dirty_and_untracked(project: Path) -> None:
    for args in (
        ["init"],
        ["add", "source.py"],
        ["-c", "user.name=Example", "-c", "user.email=example@example.com", "commit", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=project, timeout=30, check=True, capture_output=True)
    (project / "source.py").write_text("dirty", encoding="utf-8")
    (project / "untracked.py").write_text("untracked", encoding="utf-8")
    manifest = workspace.Manifest(
        "example-dev", "worker:1", "policy.yaml", "git-head", (), {}, None, False, "/sandbox/output"
    )
    assert workspace.snapshot(project, manifest, (), project / "baseline")
    assert (project / "baseline/source.py").read_text(encoding="utf-8") == "baseline\n"
    assert not (project / "baseline/untracked.py").exists()


def test_git_snapshot_excludes_native_auth_store(project: Path) -> None:
    (project / ".codex").mkdir()
    (project / ".codex/auth.json").write_text('{"refresh_token":"fictional"}', encoding="utf-8")
    for args in (
        ["init"],
        ["add", "."],
        ["-c", "user.name=Example", "-c", "user.email=example@example.com", "commit", "-m", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=project, timeout=30, check=True, capture_output=True)
    manifest = workspace.Manifest(
        "example-dev", "worker:1", "policy.yaml", "git-head", (), {}, None, False, "/sandbox/output"
    )
    workspace.snapshot(project, manifest, (), project / "baseline")
    assert not (project / "baseline/.codex/auth.json").exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cleanup_defers_cancellation(signum: int) -> None:
    steps: list[str] = []
    previous = signal.getsignal(signum)
    with pytest.raises(KeyboardInterrupt), workspace.defer_cancellation():
        steps.append("delete")
        signal.raise_signal(signum)
        steps.extend(["poll", "remove-image", "write-receipt"])
    assert steps == ["delete", "poll", "remove-image", "write-receipt"]
    assert signal.getsignal(signum) == previous


@pytest.mark.parametrize("metadata", [{"API_KEY": "fictional"}, {"CLAUDE_CODE_SKIP_VERTEX_AUTH": "token"}])
def test_environment_cannot_carry_credentials(metadata: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="nonsecret"):
        workspace.validate_environment(metadata)


def test_dry_run_does_not_run_subprocess_or_write(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("dry-run attempted subprocess")

    monkeypatch.setattr(workspace, "run", forbidden)
    assert (
        workspace.main(
            ["--project", str(project), "--manifest", "manifest.json", "--dry-run", "--", "python", "source.py"]
        )
        == 0
    )
    assert not (project / ".openshell").exists()


@pytest.mark.parametrize("provider", [None, "vertex"])
def test_provider_failure_prevents_create_and_upload(
    project: Path, monkeypatch: pytest.MonkeyPatch, provider: str | None
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, b'{"providers":[{"name":"vertex","type":"wrong"}],"next_page_token":""}', b""
        )

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.Manifest(
        "example-dev", "worker:1", "policy.yaml", "selected-files", (), {}, "google-vertex-ai", True, "/sandbox/output"
    )
    with pytest.raises(ValueError, match="provider"):
        workspace.execute(project, manifest, provider, (), project / "run", ["true"], 10)
    assert all("create" not in args and "upload" not in args for args in calls)
    assert not (project / "run").exists()


@pytest.mark.parametrize("command_exit", [0, 7])
def test_receipt_and_cleanup_on_command_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch, command_exit: int
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "download" in args:
            Path(args[-1]).mkdir()
            (Path(args[-1]) / "report.txt").write_text("worker output", encoding="utf-8")
        return subprocess.CompletedProcess(
            args,
            command_exit if "exec" in args else 0,
            b"output" if "exec" in args else b"",
            b"failure" if "exec" in args else b"",
        )

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.load_manifest(project, "manifest.json")
    output = project / "run"
    assert (
        workspace.execute(project, manifest, None, manifest.inputs, output, ["python", "source.py"], 10) == command_exit
    )
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["command_exit"] == command_exit
    assert receipt["cleanup_verified"] is True
    assert receipt["result_downloaded"] is True
    assert "MODEL" not in receipt
    assert (output / "input/source.py").read_text(encoding="utf-8") == "baseline\n"
    assert any("delete" in args for args in calls)
    assert receipt["image_cleanup_verified"] is True


def test_create_failure_still_deletes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "create" in args:
            raise RuntimeError("create failed")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.load_manifest(project, "manifest.json")
    with pytest.raises(RuntimeError, match="create failed"):
        workspace.execute(project, manifest, None, (), project / "run", ["true"], 10)
    assert any("delete" in args for args in calls)
    receipt = json.loads((project / "run/receipt.json").read_text(encoding="utf-8"))
    assert receipt["command_exit"] is None
    assert receipt["result_downloaded"] is False
    assert receipt["cleanup_verified"] is True


@pytest.mark.parametrize("cancelled", [False, True])
def test_cleanup_failure_keeps_receipt_truthful(
    project: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "delete" in args:
            if cancelled:
                signal.raise_signal(signal.SIGTERM)
            raise RuntimeError("deletion rejected")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.load_manifest(project, "manifest.json")
    with pytest.raises(RuntimeError, match="cleanup not verified"):
        workspace.execute(project, manifest, None, (), project / "run", ["true"], 10)
    receipt = json.loads((project / "run/receipt.json").read_text(encoding="utf-8"))
    assert receipt["cleanup_verified"] is False
    assert receipt["command_exit"] == 0
    assert receipt["cleanup_error"] == "RuntimeError"


def test_git_snapshot_rejects_committed_symlink(project: Path) -> None:
    (project / "link.py").symlink_to("source.py")
    for args in (
        ["init"],
        ["add", "source.py", "link.py"],
        ["-c", "user.name=Example", "-c", "user.email=example@example.com", "commit", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=project, timeout=30, check=True, capture_output=True)
    manifest = workspace.Manifest(
        "example-dev", "worker:1", "policy.yaml", "git-head", (), {}, None, False, "/sandbox/output"
    )
    with pytest.raises(ValueError, match="link or special"):
        workspace.snapshot(project, manifest, (), project / "baseline")


def test_vertex_mode_is_nonsecret(project: Path) -> None:
    path = project / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["environment"] = {"CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert workspace.load_manifest(project, "manifest.json").environment == data["environment"]


def test_selected_input_preserves_executable_mode(project: Path) -> None:
    source = project / "source.py"
    source.chmod(0o755)
    manifest = workspace.load_manifest(project, "manifest.json")
    assert workspace.snapshot(project, manifest, ("source.py",), project / "baseline") is None
    assert (project / "baseline/source.py").stat().st_mode & 0o777 == 0o755


def test_download_link_is_not_published(tmp_path: Path) -> None:
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    download = tmp_path / "quarantine"
    download.mkdir()
    (download / "report.md").symlink_to(outside)
    with pytest.raises(ValueError, match="link or special"):
        workspace.publish_results(download, tmp_path / "result")
    assert not (tmp_path / "result").exists()
    assert outside.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("stage", ["create", "build", "download"])
def test_control_failure_identifies_stage(project: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if stage in args:
            raise subprocess.CalledProcessError(3, args, b"", b"policy rejected\n")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.load_manifest(project, "manifest.json")
    with pytest.raises(RuntimeError, match="build-image failed" if stage == "build" else f"{stage} failed"):
        workspace.execute(project, manifest, None, (), project / "run", ["true"], 10)
    receipt = json.loads((project / "run/receipt.json").read_text(encoding="utf-8"))
    assert receipt["failed_stage"] == ("build-image" if stage == "build" else stage)
    assert receipt["control_exit"] == 3
    assert receipt["cleanup_verified"] is (stage != "build")
    assert receipt["image_cleanup_verified"] is True
    assert (project / "run/control-error.log").read_text(encoding="utf-8") == "policy rejected\n"


def test_interruption_still_cleans_sandbox(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "exec" in args:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(workspace, "run", fake_run)
    manifest = workspace.load_manifest(project, "manifest.json")
    with pytest.raises(KeyboardInterrupt):
        workspace.execute(project, manifest, None, (), project / "run", ["true"], 10)
    assert any("delete" in command for command in calls)
    assert json.loads((project / "run/receipt.json").read_text(encoding="utf-8"))["cleanup_verified"] is True
