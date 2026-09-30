"""Derive and evaluate consumer-visible release artifacts without publishing."""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts import _release_bundle_evidence, release_bundle, release_filesystem, release_wheelhouse
from scripts.artifact_limits import MAX_ARTIFACT_BYTES
from scripts.json_policy import load_json_bytes

_MAX_REPORT_BYTES = 5 * 1024 * 1024
_SHA256_LENGTH = 64
_WHEELHOUSE_ARCHIVE = "runtime-wheelhouse.zip"
_WHEELHOUSE_MANIFEST = "runtime-wheelhouse.json"
_INSTALL_TIMEOUT_SECONDS = 300
_CHECK_TIMEOUT_SECONDS = 60
_OFFLINE_SCENARIOS = (
    ("CONSUMER001", ("python", "-m", "venv", "<venv>")),
    ("CONSUMER002", ("python", "-m", "pip", "install", "runtime requirements")),
    ("CONSUMER003", ("python", "-m", "pip", "install", "artifact")),
    ("CONSUMER101", ("fieldkit", "--version")),
    ("CONSUMER102", ("fieldkit", "--help")),
    ("CONSUMER103", ("fieldkit", "init", "--minimal", "<workspace>")),
    ("CONSUMER104", ("fieldkit", "doctor", "--json")),
    ("CONSUMER105", ("fieldkit", "skill", "list")),
)


@dataclass(frozen=True, order=True)
class ExpectedArtifact:
    """One exact distribution the public index must expose."""

    kind: Literal["wheel", "sdist"]
    name: str
    sha256: str


@dataclass(frozen=True)
class ExpectedRelease:
    """Consumer-side identity derived from a verified candidate boundary."""

    repository: str
    planned_tag: str
    source_commit: str
    artifacts: tuple[ExpectedArtifact, ...]
    bundle_report: release_bundle.BundleReport | None = None


@dataclass(frozen=True)
class ObservedArtifact:
    """One artifact advertised by an index before download verification."""

    name: str
    sha256: str
    url: str | None = None


@dataclass(frozen=True)
class IndexReport:
    """The non-installing result of one public-index observation."""

    status: Literal["success", "pending", "failed"]
    findings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return whether both expected artifacts were observed exactly."""
        return self.status == "success"


@dataclass(frozen=True)
class PollResult:
    """The final exact observation and its bounded polling classification."""

    observed: tuple[ObservedArtifact, ...]
    report: IndexReport


class IndexTransportError(ValueError):
    """A retryable package-index transport failure."""


@dataclass(frozen=True)
class DownloadedArtifact:
    """One digest-verified artifact materialized beneath a caller-owned root."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class OfflineDependencies:
    """Caller-authenticated files for one interpreter/platform dependency closure.

    Digests must come from verified candidate evidence, never from the files being
    checked. Wheels include runtime and release-build dependencies for this target.
    """

    artifact: ExpectedArtifact
    wheels: tuple[DownloadedArtifact, ...]
    runtime_requirements: DownloadedArtifact
    release_build_requirements: DownloadedArtifact


@dataclass(frozen=True)
class LocalClosure:
    """Private, digest-checked snapshot consumed by offline installers."""

    artifact: Path
    kind: Literal["wheel", "sdist"]
    wheelhouse: Path
    runtime_requirements: Path
    release_build_requirements: Path


def _open_source_file(path: Path) -> int:
    parent_fd = release_filesystem.open_real_directory(path.parent)
    try:
        return os.open(path.name, release_filesystem.file_flags(), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _copy_verified_file(source: DownloadedArtifact, destination: Path) -> None:
    descriptor = _open_source_file(source.path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("offline closure requires bounded regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES or sha256(data).hexdigest() != source.sha256:
            raise ValueError("offline closure digest mismatch")
        destination.write_bytes(data)
    finally:
        os.close(descriptor)


def validate_hashed_requirements(path: Path) -> None:
    """Require nonempty exact hashed pins without installer directives."""
    text = path.read_text(encoding="utf-8")
    logical = text.replace("\\\n", " ")
    requirement_pattern = (
        r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.!+-]+"
        r"(?:\s*;\s*[A-Za-z0-9_ .'\"<>=!() -]+)?"
    )
    found = False
    for raw_line in logical.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, separator, hashes = line.partition(" --hash=")
        if (
            not separator
            or re.fullmatch(requirement_pattern, requirement.strip()) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}(?:\s+--hash=sha256:[0-9a-f]{64})*", hashes.strip()) is None
        ):
            raise ValueError("offline requirements must contain only hashed exact package pins")
        found = True
    if not found:
        raise ValueError("offline requirements must not be empty")


def snapshot_offline_closure(artifact: Path, dependencies: OfflineDependencies, destination: Path) -> LocalClosure:
    """Copy bounded verified bytes once, rejecting links and ambiguous wheel names."""
    expected = dependencies.artifact
    if artifact.name != expected.name or expected.kind not in {"wheel", "sdist"}:
        raise ValueError("offline artifact does not match the expected release")
    suffix = ".whl" if expected.kind == "wheel" else ".tar.gz"
    if not artifact.name.endswith(suffix):
        raise ValueError("offline artifact kind does not match its filename")
    names = [wheel.path.name for wheel in dependencies.wheels]
    if (
        not names
        or len(names) > release_wheelhouse.MAX_WHEELHOUSE_MEMBERS
        or len(set(names)) != len(names)
        or any(not name.endswith(".whl") for name in names)
    ):
        raise ValueError("offline wheelhouse requires unique wheel files")
    destination.mkdir()
    wheelhouse = destination / "wheels"
    wheelhouse.mkdir()
    copied_artifact = destination / expected.name
    _copy_verified_file(DownloadedArtifact(artifact, expected.sha256), copied_artifact)
    wheel_bytes = 0
    for wheel in dependencies.wheels:
        _copy_verified_file(wheel, wheelhouse / wheel.path.name)
        wheel_bytes += (wheelhouse / wheel.path.name).stat().st_size
        if wheel_bytes > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
            raise ValueError("offline wheelhouse exceeds the size limit")
    runtime = destination / "runtime-requirements.txt"
    build = destination / "release-build-requirements.txt"
    _copy_verified_file(dependencies.runtime_requirements, runtime)
    _copy_verified_file(dependencies.release_build_requirements, build)
    validate_hashed_requirements(runtime)
    validate_hashed_requirements(build)
    return LocalClosure(copied_artifact, expected.kind, wheelhouse, runtime, build)


@dataclass(frozen=True)
class OfflineScenarioResult:
    """One isolated consumer scenario with an explicit execution outcome."""

    id: str
    argv: tuple[str, ...]
    status: Literal["pass", "fail", "not_run"]
    exit_status: int | None


def _read_regular(path: Path) -> bytes:
    try:
        descriptor = _open_source_file(path)
    except OSError as error:
        raise ValueError("candidate report is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_REPORT_BYTES:
            raise ValueError("candidate report must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_REPORT_BYTES + 1)
        if len(data) > _MAX_REPORT_BYTES:
            raise ValueError("candidate report must be a bounded regular file")
        return data
    finally:
        os.close(descriptor)


def extract_runtime_wheelhouse(bundle: Path, candidate_report: Path, destination: Path) -> Path:
    """Extract a verified wheelhouse archive into a fresh caller-owned directory."""
    captured = _capture_bundle(bundle, candidate_report)
    return _extract_wheelhouse_bytes(captured.members[_WHEELHOUSE_ARCHIVE], destination)


def _capture_bundle(bundle: Path, candidate_report: Path) -> release_bundle.VerifiedBundle[release_bundle.BundleReport]:
    report_bytes = _read_regular(candidate_report)
    descriptor = release_filesystem.open_real_directory(bundle)
    try:
        return release_bundle.capture_open_bundle(descriptor, candidate_report=report_bytes)
    finally:
        os.close(descriptor)


def _extract_wheelhouse_bytes(data: bytes, destination: Path) -> Path:
    if len(data) > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
        raise ValueError("wheelhouse archive exceeds the size limit")
    if destination.exists() or destination.is_symlink():
        raise ValueError("wheelhouse destination must not already exist")
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > release_wheelhouse.MAX_WHEELHOUSE_MEMBERS:
                raise ValueError("wheelhouse archive has an invalid member count")
            names = [entry.filename for entry in entries]
            if names[0] != _WHEELHOUSE_MANIFEST or len(names) != len(set(names)):
                raise ValueError("wheelhouse archive manifest is invalid")
            total_bytes = 0
            for entry in entries:
                path = Path(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or entry.is_dir()
                    or entry.external_attr >> 16 & 0o170000 == stat.S_IFLNK
                ):
                    raise ValueError("wheelhouse archive has an unsafe member")
                total_bytes += entry.file_size
                if total_bytes > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
                    raise ValueError("wheelhouse archive exceeds the size limit")
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
        actual = {
            path.relative_to(destination).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in destination.rglob("*")
            if path.is_file() and path != destination / _WHEELHOUSE_MANIFEST
        }
        release_wheelhouse.validate_manifest((destination / _WHEELHOUSE_MANIFEST).read_bytes(), actual)
        return destination
    except BaseException:
        for path in sorted(destination.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        destination.rmdir()
        raise


def _runtime_target_name(*, system: str, machine: str, python_version: str) -> str:
    family = (
        "linux-x86_64"
        if system == "linux" and machine == "x86_64"
        else "macos-arm64"
        if system == "darwin" and machine == "arm64"
        else None
    )
    if family is None:
        raise ValueError("offline wheelhouse has no target for this platform")
    name = f"{family}-python{python_version.replace('.', '')}"
    if name not in {target.name for target in release_wheelhouse.supported_targets()}:
        raise ValueError("offline wheelhouse has no target for this Python version")
    return name


def install_offline(
    bundle: Path,
    candidate_report: Path,
    artifact: DownloadedArtifact,
    expected: ExpectedArtifact,
    python: Path,
    wheelhouse_destination: Path,
    *,
    system: str,
    machine: str,
    python_version: str,
    expected_bundle_report: release_bundle.BundleReport,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
    """Install captured candidate bytes without an index or mutable source paths.

    wheelhouse_destination reserves a fresh location; private sibling snapshots
    are retained only through installation and removed on success or failure.
    """
    if expected_bundle_report is None:
        raise ValueError("initial bundle verification binding is required for offline installation")
    if artifact.path.name != expected.name or artifact.sha256 != expected.sha256:
        raise ValueError("offline artifact does not match the expected release")
    target = _runtime_target_name(system=system, machine=machine, python_version=python_version)
    if wheelhouse_destination.exists() or wheelhouse_destination.is_symlink():
        raise ValueError("wheelhouse destination must not already exist")
    wheelhouse_destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"{wheelhouse_destination.name}-", dir=wheelhouse_destination.parent.absolute()
    ) as temporary:
        root = Path(temporary)
        artifact_path = root / expected.name
        _copy_verified_file(artifact, artifact_path)
        captured = _capture_bundle(bundle, candidate_report)
        if captured.report != expected_bundle_report:
            raise ValueError("offline bundle does not match the initially verified candidate")
        suffix = ".whl" if expected.kind == "wheel" else ".tar.gz"
        if (
            not expected.name.endswith(suffix)
            or dict(captured.report.files).get(expected.name) != expected.sha256
            or expected.name not in captured.members
        ):
            raise ValueError("offline artifact does not match the authenticated candidate bundle")
        wheelhouse = _extract_wheelhouse_bytes(captured.members[_WHEELHOUSE_ARCHIVE], root / "wheelhouse")
        requirements = root / "runtime-requirements.txt"
        build_requirements = root / "release-build-requirements.txt"
        requirements.write_bytes(captured.members[requirements.name])
        build_requirements.write_bytes(captured.members[build_requirements.name])
        validate_hashed_requirements(requirements)
        validate_hashed_requirements(build_requirements)
        return install_local_closure(
            LocalClosure(artifact_path, expected.kind, wheelhouse / target, requirements, build_requirements),
            python,
            cwd=cwd,
            env=env,
            runner=runner,
        )


def install_local_closure(
    closure: LocalClosure,
    python: Path,
    *,
    uv: str | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
    """Install hashed wheels then the exact artifact, without indexes or caches."""
    prefix = [uv, "pip"] if uv is not None else [str(python), "-m", "pip"]
    interpreter = ["--python", str(python)] if uv is not None else []
    offline_env = {
        **{
            name: value
            for name, value in (os.environ if env is None else env).items()
            if not name.startswith(("PIP_", "UV_"))
        },
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_OFFLINE": "1",
        "UV_NO_CACHE": "1",
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    dependencies = runner(
        [
            *prefix,
            "install",
            *interpreter,
            "--no-index",
            "--find-links",
            str(closure.wheelhouse),
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(closure.runtime_requirements),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        cwd=cwd,
        env=offline_env,
    )
    if dependencies.returncode != 0:
        return dependencies, None
    if closure.kind == "sdist":
        build = runner(
            [
                *prefix,
                "install",
                *interpreter,
                "--no-index",
                "--find-links",
                str(closure.wheelhouse),
                "--require-hashes",
                "--no-deps",
                "--requirement",
                str(closure.release_build_requirements),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            cwd=cwd,
            env=offline_env,
        )
        if build.returncode != 0:
            return dependencies, build
    artifact_result = runner(
        [
            *prefix,
            "install",
            *interpreter,
            "--no-index",
            "--no-deps",
            *(["--no-build-isolation"] if closure.kind == "sdist" else []),
            str(closure.artifact),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        cwd=cwd,
        env=offline_env,
    )
    if artifact_result.returncode != 0:
        return dependencies, artifact_result
    checked = runner(
        [*prefix, "check", *interpreter],
        capture_output=True,
        text=True,
        check=False,
        timeout=_CHECK_TIMEOUT_SECONDS,
        cwd=cwd,
        env=offline_env,
    )
    return dependencies, artifact_result if checked.returncode == 0 else checked


def _scenario_result(index: int, result: subprocess.CompletedProcess[str] | None) -> OfflineScenarioResult:
    """Bind one subprocess result to its committed, path-free scenario definition."""
    scenario_id, argv = _OFFLINE_SCENARIOS[index]
    if result is None:
        return OfflineScenarioResult(scenario_id, argv, "not_run", None)
    return OfflineScenarioResult(scenario_id, argv, "pass" if result.returncode == 0 else "fail", result.returncode)


def run_offline_scenarios(
    bundle: Path,
    candidate_report: Path,
    artifact: DownloadedArtifact,
    expected: ExpectedArtifact,
    *,
    system: str,
    machine: str,
    python_version: str,
    expected_bundle_report: release_bundle.BundleReport,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[OfflineScenarioResult, ...]:
    """Run fixed-argv user scenarios after one isolated offline installation."""
    if expected_bundle_report is None:
        raise ValueError("initial bundle verification binding is required for offline scenarios")
    with tempfile.TemporaryDirectory(prefix="fieldkit-consumer-") as raw_root:
        root = Path(raw_root)
        home = root / "home"
        workspace = root / "workspace"
        run_dir = root / "run"
        run_dir.mkdir()
        environment = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "PYTHONSAFEPATH": "1",
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "FIELDKIT_NO_LLM": "1",
        }
        venv = root / "venv"
        create = runner(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            cwd=run_dir,
            env=environment,
        )
        results = [_scenario_result(0, create)]
        if create.returncode != 0:
            return tuple(results + [_scenario_result(index, None) for index in range(1, len(_OFFLINE_SCENARIOS))])
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        fieldkit = scripts / ("fieldkit.exe" if os.name == "nt" else "fieldkit")
        dependencies, installed = install_offline(
            bundle,
            candidate_report,
            artifact,
            expected,
            python,
            root / "wheelhouse",
            system=system,
            machine=machine,
            python_version=python_version,
            expected_bundle_report=expected_bundle_report,
            cwd=run_dir,
            env=environment,
            runner=runner,
        )
        results.append(_scenario_result(1, dependencies))
        if installed is None:
            return tuple(results + [_scenario_result(index, None) for index in range(2, len(_OFFLINE_SCENARIOS))])
        results.append(_scenario_result(2, installed))
        if installed.returncode != 0:
            return tuple(results + [_scenario_result(index, None) for index in range(3, len(_OFFLINE_SCENARIOS))])
        commands = (
            [str(fieldkit), "--version"],
            [str(fieldkit), "--help"],
            [str(fieldkit), "init", "--minimal", str(workspace)],
            [str(fieldkit), "doctor", "--json"],
            [str(fieldkit), "skill", "list"],
        )
        for index, command in enumerate(commands, start=3):
            result = runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
                cwd=run_dir,
                env=environment,
            )
            results.append(_scenario_result(index, result))
        return tuple(results)


def offline_scenario_evidence(
    results: tuple[OfflineScenarioResult, ...], *, artifact_name: str
) -> tuple[dict[str, object], ...]:
    """Render path-free fixed-argv offline scenario evidence."""
    if len(results) != len(_OFFLINE_SCENARIOS):
        raise ValueError("offline scenario evidence is incomplete")
    if not artifact_name or any(
        result.id != scenario_id or result.argv != argv
        for result, (scenario_id, argv) in zip(results, _OFFLINE_SCENARIOS, strict=True)
    ):
        raise ValueError("offline scenario evidence does not match the committed scenarios")
    return tuple(
        {
            "artifact_name": artifact_name,
            "id": result.id,
            "argv": list(result.argv),
            "exit_status": result.exit_status,
            "status": result.status,
        }
        for result in results
    )


def _candidate_identity(data: bytes) -> tuple[str, str, tuple[ExpectedArtifact, ...]]:
    try:
        report = load_json_bytes(data)
    except ValueError:
        raise ValueError("candidate report is invalid JSON") from None
    if not isinstance(report, dict):
        raise ValueError("candidate report must be an object")
    repository = report.get("expected_repository")
    planned_tag = report.get("planned_tag")
    validation = report.get("artifact_validation")
    if not isinstance(repository, str) or not isinstance(planned_tag, str) or not isinstance(validation, dict):
        raise ValueError("candidate report has no release identity")
    artifacts = validation.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("candidate report has no validated artifacts")
    expected: list[ExpectedArtifact] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("candidate artifact is invalid")
        name = artifact.get("name")
        kind = artifact.get("kind")
        digest = artifact.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(kind, str)
            or kind not in {"wheel", "sdist"}
            or not isinstance(digest, str)
            or len(digest) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("candidate artifact is invalid")
        expected.append(ExpectedArtifact(cast(Literal["wheel", "sdist"], kind), name, digest))
    if len(expected) != 2 or {artifact.kind for artifact in expected} != {"wheel", "sdist"}:
        raise ValueError("candidate must contain exactly one wheel and one sdist")
    return repository, planned_tag, tuple(sorted(expected))


def expected_release(bundle: Path, candidate_report: Path) -> ExpectedRelease:
    """Return expected consumer artifacts only after verifying the closed bundle."""
    candidate_bytes = _read_regular(candidate_report)
    descriptor = release_filesystem.open_real_directory(bundle)
    try:
        bundle_report = release_bundle.capture_open_bundle(descriptor, candidate_report=candidate_bytes).report
    finally:
        os.close(descriptor)
    repository, planned_tag, artifacts = _candidate_identity(candidate_bytes)
    bundle_artifacts = {(name, digest) for name, digest in bundle_report.files if name.endswith((".whl", ".tar.gz"))}
    if {(artifact.name, artifact.sha256) for artifact in artifacts} != bundle_artifacts:
        raise ValueError("candidate artifacts do not match the verified bundle")
    return ExpectedRelease(repository, planned_tag, bundle_report.source_commit, artifacts, bundle_report)


def _https_host(url: str, subject: str) -> str:
    """Return one unambiguous HTTPS hostname for an index input URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{subject} must use HTTPS")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{subject} host is invalid") from error
    if parsed.username is not None or parsed.password is not None or parsed.hostname is None:
        raise ValueError(f"{subject} host is invalid")
    if port not in {None, 443}:
        raise ValueError(f"{subject} must use the default HTTPS port")
    return parsed.hostname.lower()


def index_observations(
    payload: object, *, endpoint: str, allowed_download_hosts: frozenset[str]
) -> tuple[ObservedArtifact, ...]:
    """Parse one trusted-transport index response under explicit host policy."""
    _https_host(endpoint, "index endpoint")
    allowed_hosts = {host.lower() for host in allowed_download_hosts}
    if not allowed_hosts:
        raise ValueError("download host policy is empty")
    if not isinstance(payload, dict) or not isinstance(payload.get("urls"), list):
        raise ValueError("index response has an unsupported schema")
    observed: list[ObservedArtifact] = []
    for value in payload["urls"]:
        if not isinstance(value, dict):
            raise ValueError("index artifact has an unsupported schema")
        name = value.get("filename")
        digests = value.get("digests")
        url = value.get("url")
        if (
            not isinstance(name, str)
            or not isinstance(digests, dict)
            or not isinstance(digests.get("sha256"), str)
            or not isinstance(url, str)
        ):
            raise ValueError("index artifact has an unsupported schema")
        digest = digests["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("index artifact digest is invalid")
        if _https_host(url, "artifact URL") not in allowed_hosts:
            raise ValueError("artifact URL host is not allowed")
        observed.append(ObservedArtifact(name, digest, url))
    return tuple(observed)


class _ClosableResponse(Protocol):
    def close(self) -> None: ...


class _RejectRedirects(HTTPRedirectHandler):
    """Reject redirects before urllib can contact another URL or drain its body."""

    def redirect_request(
        self, req: Request, fp: _ClosableResponse, code: int, msg: str, headers: object, newurl: str
    ) -> Request | None:
        fp.close()
        raise ValueError("artifact redirects are not allowed")


def fetch_https_bytes(url: str, *, timeout_seconds: float, allowed_download_hosts: frozenset[str]) -> bytes:
    """Fetch bounded bytes from an allowed HTTPS host without following redirects."""
    if timeout_seconds <= 0:
        raise ValueError("download timeout must be positive")
    allowed_hosts = {host.lower() for host in allowed_download_hosts}
    if _https_host(url, "artifact URL") not in allowed_hosts:
        raise ValueError("artifact URL host is not allowed")
    try:
        with build_opener(_RejectRedirects()).open(url, timeout=timeout_seconds) as response:
            if _https_host(response.geturl(), "artifact URL") not in allowed_hosts:
                raise ValueError("artifact URL host is not allowed")
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as error:
        raise IndexTransportError("artifact download failed") from error
    if not isinstance(payload, bytes) or len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("download payload is invalid or exceeds the size limit")
    return payload


def fetch_index_observations(
    endpoint: str, *, timeout_seconds: float, allowed_download_hosts: frozenset[str]
) -> tuple[ObservedArtifact, ...]:
    """Fetch and parse one bounded release-specific index response."""
    endpoint_host = _https_host(endpoint, "index endpoint")
    payload = fetch_https_bytes(
        endpoint,
        timeout_seconds=timeout_seconds,
        allowed_download_hosts=frozenset({endpoint_host}),
    )
    try:
        parsed = load_json_bytes(payload)
    except ValueError:
        raise ValueError("index response is invalid JSON") from None
    return index_observations(parsed, endpoint=endpoint, allowed_download_hosts=allowed_download_hosts)


def download_verified(
    observed: ObservedArtifact,
    expected: ExpectedArtifact,
    destination: Path,
    *,
    fetch: Callable[[str, float], bytes],
    timeout_seconds: float = 30.0,
) -> DownloadedArtifact:
    """Download one expected artifact and atomically retain only verified bytes."""
    if observed.name != expected.name or observed.sha256 != expected.sha256 or observed.url is None:
        raise ValueError("observed artifact does not match the expected release artifact")
    if timeout_seconds <= 0:
        raise ValueError("download timeout must be positive")
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("download destination must be a directory")
    payload = fetch(observed.url, timeout_seconds)
    if not isinstance(payload, bytes) or len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("download payload is invalid or exceeds the size limit")
    digest = sha256(payload).hexdigest()
    if digest != expected.sha256:
        raise ValueError("download digest does not match the expected release artifact")
    target = destination / expected.name
    if target.exists() or target.is_symlink():
        raise ValueError("download destination already contains the expected artifact")
    with tempfile.NamedTemporaryFile(dir=destination, prefix=".release-download-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    return DownloadedArtifact(target, digest)


def download_expected_artifacts(
    expected: ExpectedRelease,
    observed: tuple[ObservedArtifact, ...],
    destination: Path,
    *,
    fetch: Callable[[str, float], bytes],
    timeout_seconds: float = 30.0,
) -> tuple[DownloadedArtifact, ...]:
    """Retain both artifacts only after an exact successful index observation."""
    report = evaluate_index(expected, observed)
    if not report.ok:
        raise ValueError("index observation is not an exact success")
    observed_by_name = {artifact.name: artifact for artifact in observed}
    downloaded: list[DownloadedArtifact] = []
    try:
        for artifact in expected.artifacts:
            downloaded.append(
                download_verified(
                    observed_by_name[artifact.name],
                    artifact,
                    destination,
                    fetch=fetch,
                    timeout_seconds=timeout_seconds,
                )
            )
    except (OSError, ValueError):
        for downloaded_artifact in downloaded:
            downloaded_artifact.path.unlink(missing_ok=True)
        raise
    return tuple(downloaded)


def evaluate_index(expected: ExpectedRelease, observed: tuple[ObservedArtifact, ...]) -> IndexReport:
    """Classify one index observation without downloading or installing files."""
    if not observed:
        return IndexReport("pending", ("expected version is not visible",))
    observed_by_name: dict[str, ObservedArtifact] = {}
    findings: list[str] = []
    for observation in observed:
        if observation.name in observed_by_name:
            findings.append(f"duplicate index artifact: {observation.name}")
        else:
            observed_by_name[observation.name] = observation
    expected_by_name = {artifact.name: artifact for artifact in expected.artifacts}
    for artifact in expected.artifacts:
        index_artifact = observed_by_name.get(artifact.name)
        if index_artifact is None:
            findings.append(f"missing expected {artifact.kind} artifact")
        elif index_artifact.sha256 != artifact.sha256:
            findings.append(f"digest mismatch for {artifact.name}")
    for name in sorted(set(observed_by_name) - set(expected_by_name)):
        findings.append(f"unexpected index artifact: {name}")
    return IndexReport("failed" if findings else "success", tuple(sorted(findings)))


def poll_index_observations(
    expected: ExpectedRelease,
    *,
    fetch: Callable[[], tuple[ObservedArtifact, ...]],
    max_attempts: int,
    poll_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> PollResult:
    """Poll a bounded number of times and return the first exact or final classification."""
    if max_attempts < 1:
        raise ValueError("poll attempts must be positive")
    if poll_interval_seconds < 0:
        raise ValueError("poll interval must not be negative")
    latest: tuple[ObservedArtifact, ...] = ()
    for attempt in range(max_attempts):
        try:
            latest = fetch()
        except IndexTransportError:
            if attempt + 1 == max_attempts:
                return PollResult((), IndexReport("pending", (f"index unavailable after {max_attempts} attempts",)))
        else:
            report = evaluate_index(expected, latest)
            if report.ok:
                return PollResult(latest, report)
        if attempt + 1 < max_attempts and poll_interval_seconds:
            sleep(poll_interval_seconds)
    return PollResult(latest, evaluate_index(expected, latest))


def consumer_evidence(
    expected: ExpectedRelease,
    *,
    endpoint: str,
    observed: tuple[ObservedArtifact, ...],
    report: IndexReport,
    system: str,
    machine: str,
    python_version: str,
    downloaded: tuple[DownloadedArtifact, ...] = (),
    scenarios: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """Render schema-valid, same-candidate evidence for one index observation."""
    _https_host(endpoint, "index endpoint")
    if not system or not machine or not python_version:
        raise ValueError("consumer environment is incomplete")
    expected_downloads = {(artifact.name, artifact.sha256) for artifact in expected.artifacts}
    received_downloads = {(artifact.path.name, artifact.sha256) for artifact in downloaded}
    if received_downloads and received_downloads != expected_downloads:
        raise ValueError("download receipts must match every expected artifact")
    downloaded_names = {artifact.path.name for artifact in downloaded}
    observed_names = {artifact.name for artifact in observed}
    status = report.status
    findings = list(report.findings)
    if report.ok:
        if not downloaded:
            status = "pending"
            findings.append("download verification not requested")
        elif not scenarios:
            status = "pending"
            findings.append("offline scenario verification not requested")
        else:
            expected_scenarios = {
                (artifact.name, scenario_id): list(argv)
                for artifact in expected.artifacts
                for scenario_id, argv in _OFFLINE_SCENARIOS
            }
            observed_scenarios: set[tuple[str, str]] = set()
            failures: list[str] = []
            for scenario in scenarios:
                if set(scenario) != {"artifact_name", "id", "argv", "exit_status", "status"}:
                    raise ValueError("offline scenario has an unsupported schema")
                artifact_name = scenario["artifact_name"]
                scenario_id = scenario["id"]
                argv = scenario["argv"]
                scenario_status = scenario["status"]
                exit_status = scenario["exit_status"]
                if not isinstance(artifact_name, str) or not isinstance(scenario_id, str):
                    raise ValueError("offline scenario evidence is invalid")
                key = (artifact_name, scenario_id)
                if (
                    key not in expected_scenarios
                    or key in observed_scenarios
                    or argv != expected_scenarios[key]
                    or scenario_status not in {"pass", "fail", "not_run"}
                    or (scenario_status == "pass" and exit_status != 0)
                    or (scenario_status == "fail" and (type(exit_status) is not int or exit_status == 0))
                    or (scenario_status == "not_run" and exit_status is not None)
                ):
                    raise ValueError("offline scenario evidence is invalid")
                observed_scenarios.add(key)
                if scenario_status != "pass":
                    failures.append(f"offline scenario did not pass: {artifact_name}/{scenario_id}")
            if observed_scenarios != set(expected_scenarios):
                status = "pending"
                findings.append("offline scenario evidence is incomplete")
            elif failures:
                status = "failed"
                findings.extend(sorted(failures))
    return {
        "schema_version": 5,
        "status": status,
        "repository": expected.repository,
        "source_commit": expected.source_commit,
        "planned_tag": expected.planned_tag,
        "index_endpoint": endpoint,
        "environment": {"system": system, "machine": machine, "python_version": python_version},
        "artifacts": [
            {
                "kind": artifact.kind,
                "name": artifact.name,
                "sha256": artifact.sha256,
                "observed": artifact.name in observed_names,
                "downloaded": artifact.name in downloaded_names,
            }
            for artifact in expected.artifacts
        ],
        "scenarios": list(scenarios),
        "findings": findings,
    }
