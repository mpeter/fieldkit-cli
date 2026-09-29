"""Run an explicitly selected project snapshot in an existing OpenShell workspace."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import click

CLEANUP_TIMEOUT = 60
CONTROL_TIMEOUT = 60
GIT_TIMEOUT = 60
TRANSFER_TIMEOUT = 120
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
SECRET_NAME = re.compile(
    r"secret|token|password|credential|api[_-]?key|private[_-]?key|access[_-]?key|cookie|bearer|auth", re.I
)
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
IMAGE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:@_-]*\Z")
SECRET_VALUE = re.compile(r"-----BEGIN|(?:sk-|ghp_|github_pat_|AIza)[A-Za-z0-9_-]{12,}")


@dataclass(frozen=True)
class Manifest:
    workspace: str
    image: str
    policy: str
    snapshot: str
    inputs: tuple[str, ...]
    environment: dict[str, str]
    provider_type: str | None
    provider_required: bool
    result_path: str


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or path == PurePosixPath("."):
        raise ValueError("paths must be nonempty relative paths without traversal")
    return path


def sensitive(path: PurePosixPath) -> bool:
    return any(
        part.lower()
        in {
            ".git",
            ".ssh",
            ".aws",
            ".gnupg",
            "credentials",
            "credentials.json",
            "auth.json",
            "id_rsa",
            "id_ed25519",
            ".npmrc",
            ".netrc",
            ".pypirc",
            "service-account.json",
        }
        or part.lower().startswith(".env")
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        or "credential" in part.lower()
        for part in path.parts
    )


def local_path(project: Path, value: str, *, input_file: bool = True) -> Path:
    relative = relative_path(value)
    target = project.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(project):
        raise ValueError("path escapes project")
    if any(project.joinpath(*relative.parts[:index]).is_symlink() for index in range(1, len(relative.parts) + 1)):
        raise ValueError("symlinks are not accepted")
    if input_file and (sensitive(relative) or not target.is_file()):
        raise ValueError("input must be a regular noncredential file")
    return target


def validate_environment(environment: dict[str, str]) -> None:
    for key, value in environment.items():
        if (
            not ENV_NAME.fullmatch(key)
            or (SECRET_NAME.search(key) and not (key == "CLAUDE_CODE_SKIP_VERTEX_AUTH" and value == "1"))
            or SECRET_VALUE.search(value)
            or "\n" in value
            or "\x00" in value
        ):
            raise ValueError("environment must contain only nonsecret values")


@contextmanager
def defer_cancellation() -> Iterator[None]:
    """Complete bounded cleanup before honoring an arriving termination signal."""
    pending = False

    def defer(signum: int, frame: object) -> None:
        nonlocal pending
        pending = True

    previous = {item: signal.signal(item, defer) for item in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)
    if pending:
        raise KeyboardInterrupt


def load_manifest(project: Path, name: str) -> Manifest:
    raw: object = json.loads(local_path(project, name).read_text(encoding="utf-8"))
    fields = {
        "schema_version",
        "workspace",
        "image",
        "policy",
        "snapshot",
        "inputs",
        "environment",
        "provider_type",
        "provider_required",
        "result_path",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) - fields
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
    ):
        raise ValueError("manifest must be a schema_version 1 object with only supported fields")
    for field in ("workspace", "image", "policy", "snapshot", "result_path"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"manifest requires nonempty {field}")
    workspace, image, policy, snapshot, result_path = (
        str(raw[field]) for field in ("workspace", "image", "policy", "snapshot", "result_path")
    )
    if not SAFE_NAME.fullmatch(workspace):
        raise ValueError("workspace must be a safe resource name")
    if not IMAGE_REFERENCE.fullmatch(image):
        raise ValueError("image must be a container image reference")
    if snapshot not in {"git-head", "selected-files"} or result_path not in {"/sandbox/workspace", "/sandbox/output"}:
        raise ValueError("unsupported snapshot or result_path")
    local_path(project, policy)
    inputs = raw.get("inputs", [])
    environment = raw.get("environment", {})
    provider_type = raw.get("provider_type")
    provider_required = raw.get("provider_required", False)
    if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
        raise ValueError("inputs must be a list of relative file names")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("environment must contain string names and values")
    if type(provider_required) is not bool or (
        provider_type is not None and (not isinstance(provider_type, str) or not provider_type.strip())
    ):
        raise ValueError("invalid provider fields")
    if provider_required and not provider_type:
        raise ValueError("required provider must declare provider_type")
    validate_environment(environment)
    for item in inputs:
        local_path(project, item)
    return Manifest(
        workspace,
        image,
        policy,
        snapshot,
        tuple(inputs),
        dict(environment),
        provider_type,
        provider_required,
        result_path,
    )


def run(
    args: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    checked: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, check=False, timeout=timeout, stdin=subprocess.DEVNULL
    )
    if checked and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result


def snapshot(project: Path, manifest: Manifest, inputs: tuple[str, ...], destination: Path) -> str | None:
    destination.mkdir()
    head = None
    if manifest.snapshot == "git-head":
        head = run(["git", "rev-parse", "HEAD"], cwd=project, timeout=GIT_TIMEOUT).stdout.decode().strip()
        archive = run(["git", "archive", "--format=tar", head], cwd=project, timeout=GIT_TIMEOUT).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                path = relative_path(member.name.rstrip("/"))
                if sensitive(path):
                    continue
                if not (member.isdir() or member.isfile()):
                    raise ValueError("git snapshot contains a link or special file")
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = tar.extractfile(member)
                    if stream is None:
                        raise ValueError("archive file has no contents")
                    with stream, target.open("wb") as output:
                        shutil.copyfileobj(stream, output)
                    target.chmod(member.mode & 0o777)
    for item in inputs:
        source = local_path(project, item)
        target = destination.joinpath(*relative_path(item).parts)
        if target.exists() and manifest.snapshot == "git-head":
            raise ValueError("additional inputs cannot overwrite committed snapshot files")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return head


def publish_results(download: Path, destination: Path) -> None:
    for path in (download, *download.rglob("*")):
        mode = path.lstat()
        if not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode)):
            raise ValueError("download contains a link or special file")
        if stat.S_ISREG(mode.st_mode) and mode.st_nlink != 1:
            raise ValueError("download contains hard-linked files")
    shutil.copytree(download, destination)


def cancel_workspace(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def remove_worker_image(image: str) -> None:
    presence = run(["podman", "image", "exists", image], timeout=CONTROL_TIMEOUT, checked=False)
    if presence.returncode == 1:
        return
    if presence.returncode != 0:
        raise RuntimeError("could not determine whether worker image remains")
    run(["podman", "image", "rm", image], timeout=CONTROL_TIMEOUT)


def execute(
    project: Path,
    manifest: Manifest,
    provider: str | None,
    inputs: tuple[str, ...],
    output: Path,
    command: list[str],
    timeout: int,
) -> int:
    prefix = ["openshell", "--workspace", manifest.workspace]
    run([*prefix, "status"], timeout=CONTROL_TIMEOUT)
    run([*prefix, "workspace", "get", manifest.workspace], timeout=CONTROL_TIMEOUT)
    if manifest.provider_required and not provider:
        raise ValueError("this workspace requires an explicitly selected provider")
    if provider:
        if not SAFE_NAME.fullmatch(provider):
            raise ValueError("provider must be a safe resource name")
        provider_data: object = json.loads(
            run([*prefix, "provider", "list", "-o", "json"], timeout=CONTROL_TIMEOUT).stdout
        )
        if not isinstance(provider_data, dict) or not isinstance(provider_data.get("providers"), list):
            raise ValueError("provider list response is malformed")
        matches = [
            entry for entry in provider_data["providers"] if isinstance(entry, dict) and entry.get("name") == provider
        ]
        if len(matches) != 1:
            raise ValueError("provider not found in first page; select a visible configured provider")
        if manifest.provider_type and matches[0].get("type") != manifest.provider_type:
            raise ValueError("provider type does not match manifest")
    sandbox = f"fk-{uuid.uuid4().hex[:16]}"
    worker_image = f"localhost/fieldkit-run:{sandbox}"
    receipt: dict[str, object] = {
        "workspace": manifest.workspace,
        "sandbox": sandbox,
        "image": manifest.image,
        "snapshot": manifest.snapshot,
        "provider": provider,
        "command_exit": None,
        "cleanup_verified": False,
        "result_downloaded": False,
        "worker_image": worker_image,
        "image_cleanup_verified": False,
    }
    with tempfile.TemporaryDirectory(prefix="fieldkit-openshell-") as staging:
        baseline = Path(staging) / "input"
        receipt["head"] = snapshot(project, manifest, inputs, baseline)
        output.mkdir(parents=True, exist_ok=False)
        shutil.copytree(baseline, output / "input")
        create = [
            *prefix,
            "sandbox",
            "create",
            "--name",
            sandbox,
            "--label",
            "owner=fieldkit-workspace",
            "--label",
            f"fieldkit-run={sandbox}",
            "--from",
            worker_image,
            "--policy",
            str(local_path(project, manifest.policy)),
            "--cpu",
            "1",
            "--memory",
            "2Gi",
            "--approval-mode",
            "manual",
            "--no-auto-providers",
            "--detach",
        ]
        if provider:
            create.extend(["--provider", provider])
        for key, value in manifest.environment.items():
            create.extend(["--env", f"{key}={value}"])
        attempted = False
        error: Exception | None = None
        cleanup_error: Exception | None = None
        code = 1
        stage = "build-image"
        try:
            (Path(staging) / "Dockerfile").write_text(
                f"FROM {manifest.image}\nUSER root\n"
                "COPY --chown=1001:1001 input/ /sandbox/workspace/\n"
                "USER sandbox\nWORKDIR /sandbox/workspace\n",
                encoding="utf-8",
            )
            run(
                [
                    "podman",
                    "build",
                    "--pull=never",
                    "--layers=false",
                    "--force-rm",
                    "--label",
                    f"fieldkit-run={sandbox}",
                    "--tag",
                    worker_image,
                    str(staging),
                ],
                timeout=TRANSFER_TIMEOUT,
            )
            stage = "create"
            attempted = True
            run([*create, "--", "sleep", "infinity"], timeout=CONTROL_TIMEOUT)
            stage = "command"
            result = run(
                [
                    *prefix,
                    "sandbox",
                    "exec",
                    "--name",
                    sandbox,
                    "--workdir",
                    "/sandbox/workspace",
                    "--no-login-shell",
                    "--no-tty",
                    "--timeout",
                    str(timeout),
                    "--",
                    *command,
                ],
                timeout=timeout + CONTROL_TIMEOUT,
                checked=False,
            )
            code = result.returncode
            receipt["command_exit"] = code
            (output / "stdout.log").write_bytes(result.stdout)
            (output / "stderr.log").write_bytes(result.stderr)
            stage = "download"
            quarantine = Path(staging) / "download"
            run(
                [*prefix, "sandbox", "download", sandbox, manifest.result_path, str(quarantine)],
                timeout=TRANSFER_TIMEOUT,
            )
            stage = "validate-results"
            publish_results(quarantine, output / "result")
            receipt["result_downloaded"] = True
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            error = exc
            receipt["error"] = type(exc).__name__
            receipt["failed_stage"] = stage
            if isinstance(exc, subprocess.CalledProcessError):
                receipt["control_exit"] = exc.returncode
                (output / "control-error.log").write_bytes(exc.stderr or b"")
        finally:
            with defer_cancellation():
                if attempted:
                    try:
                        run([*prefix, "sandbox", "delete", sandbox], timeout=CONTROL_TIMEOUT)
                        deadline = time.monotonic() + CLEANUP_TIMEOUT
                        while True:
                            listing = (
                                run(
                                    [*prefix, "sandbox", "list", "--selector", f"fieldkit-run={sandbox}", "--names"],
                                    timeout=CONTROL_TIMEOUT,
                                )
                                .stdout.decode()
                                .splitlines()
                            )
                            if sandbox not in listing:
                                break
                            if time.monotonic() >= deadline:
                                raise RuntimeError("sandbox remains after deletion")
                            time.sleep(1)
                        receipt["cleanup_verified"] = True
                    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                        cleanup_error = exc
                        receipt["cleanup_error"] = type(exc).__name__
                try:
                    remove_worker_image(worker_image)
                    receipt["image_cleanup_verified"] = True
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    cleanup_error = exc
                    receipt["image_cleanup_error"] = type(exc).__name__
                (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
                if cleanup_error:
                    raise RuntimeError(
                        f"cleanup not verified; inspect receipt for sandbox {sandbox}"
                    ) from cleanup_error
        if error:
            raise RuntimeError(f"workspace {stage} failed; inspect receipt and control-error.log") from error
        return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", default=".openshell/workspace.json")
    parser.add_argument("--provider")
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--env", action="append", default=[], help="nonsecret execution metadata (NAME=VALUE)")
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    previous_signal = signal.signal(signal.SIGTERM, cancel_workspace)
    try:
        project = args.project.resolve(strict=True)
        if not project.is_dir() or args.timeout <= 0:
            raise ValueError("project must be a directory and timeout must be positive")
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise ValueError("provide a command after --")
        manifest = load_manifest(project, args.manifest)
        metadata: dict[str, str] = dict(manifest.environment)
        for item in args.env:
            name, separator, value = item.partition("=")
            if not separator:
                raise ValueError("execution metadata requires NAME=VALUE")
            metadata[name] = value
        validate_environment(metadata)
        manifest = replace(manifest, environment=metadata)
        inputs = tuple(dict.fromkeys((*manifest.inputs, *args.input)))
        for item in inputs:
            local_path(project, item)
        output = local_path(project, args.output or f".openshell/runs/{uuid.uuid4().hex}", input_file=False)
        if output.exists():
            raise ValueError("output must not already exist")
        if manifest.provider_required and not args.provider:
            raise ValueError("this workspace requires an explicitly selected provider")
        if args.dry_run:
            click.echo(
                json.dumps(
                    {
                        "workspace": manifest.workspace,
                        "image": manifest.image,
                        "policy": manifest.policy,
                        "snapshot": manifest.snapshot,
                        "inputs": inputs,
                        "command": command,
                    },
                    indent=2,
                )
            )
            return 0
        return execute(project, manifest, args.provider, inputs, output, command, args.timeout)
    except KeyboardInterrupt:
        click.echo("OpenShell workspace interrupted; cleanup attempted", err=True)
        return 130
    except subprocess.TimeoutExpired:
        click.echo("OpenShell workspace: a bounded operation timed out", err=True)
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        click.echo(f"OpenShell workspace: {exc}", err=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_signal)


if __name__ == "__main__":
    raise SystemExit(main())
