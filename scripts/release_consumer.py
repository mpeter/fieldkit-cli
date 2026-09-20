"""Derive and evaluate consumer-visible release artifacts without publishing."""

from __future__ import annotations

import json
import os
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
from typing import Literal, cast
from urllib.parse import urlparse
from urllib.request import urlopen

from scripts import release_bundle, release_wheelhouse

_MAX_REPORT_BYTES = 5 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_SHA256_LENGTH = 64
_WHEELHOUSE_ARCHIVE = "runtime-wheelhouse.zip"
_WHEELHOUSE_MANIFEST = "runtime-wheelhouse.json"
_MAX_WHEELHOUSE_MEMBERS = 4096
_MAX_WHEELHOUSE_BYTES = 100 * 1024 * 1024
_INSTALL_TIMEOUT_SECONDS = 300
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
class OfflineScenarioResult:
    """One isolated consumer scenario with an explicit execution outcome."""

    id: str
    argv: tuple[str, ...]
    status: Literal["pass", "fail", "not_run"]
    exit_status: int | None


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
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
    release_bundle.verify(bundle, candidate_report=_read_regular(candidate_report))
    archive_path = bundle / _WHEELHOUSE_ARCHIVE
    if destination.exists() or destination.is_symlink():
        raise ValueError("wheelhouse destination must not already exist")
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > _MAX_WHEELHOUSE_MEMBERS:
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
                if total_bytes > _MAX_WHEELHOUSE_BYTES:
                    raise ValueError("wheelhouse archive exceeds the size limit")
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
        manifest = json.loads((destination / _WHEELHOUSE_MANIFEST).read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "targets"}
            or manifest.get("schema_version") != 1
        ):
            raise ValueError("wheelhouse manifest has an unsupported schema")
        targets = manifest["targets"]
        if not isinstance(targets, list) or len(targets) != len(release_wheelhouse.supported_targets()):
            raise ValueError("wheelhouse manifest has an unsupported target matrix")
        expected_names = [target.name for target in release_wheelhouse.supported_targets()]
        if [target.get("name") if isinstance(target, dict) else None for target in targets] != expected_names:
            raise ValueError("wheelhouse manifest has an unsupported target matrix")
        for target, expected in zip(targets, release_wheelhouse.supported_targets(), strict=True):
            if not isinstance(target, dict) or set(target) != {"name", "python_version", "platform", "wheels"}:
                raise ValueError("wheelhouse manifest target has an unsupported schema")
            if target["python_version"] != expected.python_version or target["platform"] != expected.platform:
                raise ValueError("wheelhouse manifest target does not match the supported matrix")
            wheels = target["wheels"]
            if not isinstance(wheels, list) or not wheels:
                raise ValueError("wheelhouse manifest target has no wheels")
            declared: dict[str, str] = {}
            for wheel in wheels:
                if not isinstance(wheel, dict) or set(wheel) != {"name", "sha256"}:
                    raise ValueError("wheelhouse manifest wheel has an unsupported schema")
                name = wheel["name"]
                digest = wheel["sha256"]
                if not isinstance(name, str) or not name.endswith(".whl") or not isinstance(digest, str):
                    raise ValueError("wheelhouse manifest wheel is invalid")
                if (
                    name in declared
                    or len(digest) != _SHA256_LENGTH
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError("wheelhouse manifest wheel is invalid")
                declared[name] = digest
            actual = {
                path.name: sha256(path.read_bytes()).hexdigest() for path in (destination / expected.name).iterdir()
            }
            if declared != actual:
                raise ValueError("wheelhouse manifest does not match extracted wheels")
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
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
    """Install one verified artifact in a fresh environment without any package index."""
    bundle = bundle.resolve()
    candidate_report = candidate_report.resolve()
    artifact_path = artifact.path.resolve()
    if artifact_path.name != expected.name or artifact.sha256 != expected.sha256:
        raise ValueError("offline artifact does not match the expected release")
    if sha256(artifact_path.read_bytes()).hexdigest() != expected.sha256:
        raise ValueError("offline artifact digest does not match the expected release")
    target = _runtime_target_name(system=system, machine=machine, python_version=python_version)
    wheelhouse = extract_runtime_wheelhouse(bundle, candidate_report, wheelhouse_destination)
    requirements = bundle / "runtime-requirements.txt"
    dependencies = runner(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse / target),
            "--require-hashes",
            "--requirement",
            str(requirements),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        cwd=cwd,
        env=env,
    )
    if dependencies.returncode != 0:
        return dependencies, None
    if expected.kind == "sdist":
        build_requirements = bundle / "release-build-requirements.txt"
        build = runner(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse / target),
                "--require-hashes",
                "--requirement",
                str(build_requirements),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
        if build.returncode != 0:
            return dependencies, build
    artifact_result = runner(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            *(["--no-build-isolation"] if expected.kind == "sdist" else []),
            str(artifact_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        cwd=cwd,
        env=env,
    )
    return dependencies, artifact_result


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
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[OfflineScenarioResult, ...]:
    """Run fixed-argv user scenarios after one isolated offline installation."""
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
        report = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate report is invalid JSON") from error
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
    bundle_report = release_bundle.verify(bundle, candidate_report=candidate_bytes)
    repository, planned_tag, artifacts = _candidate_identity(candidate_bytes)
    bundle_artifacts = {(name, digest) for name, digest in bundle_report.files if name.endswith((".whl", ".tar.gz"))}
    if {(artifact.name, artifact.sha256) for artifact in artifacts} != bundle_artifacts:
        raise ValueError("candidate artifacts do not match the verified bundle")
    return ExpectedRelease(repository, planned_tag, bundle_report.source_commit, artifacts)


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


def fetch_https_bytes(url: str, *, timeout_seconds: float, allowed_download_hosts: frozenset[str]) -> bytes:
    """Fetch bounded bytes and revalidate the final redirect destination."""
    if timeout_seconds <= 0:
        raise ValueError("download timeout must be positive")
    allowed_hosts = {host.lower() for host in allowed_download_hosts}
    if _https_host(url, "artifact URL") not in allowed_hosts:
        raise ValueError("artifact URL host is not allowed")
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            if _https_host(response.geturl(), "artifact URL") not in allowed_hosts:
                raise ValueError("artifact URL host is not allowed")
            payload = response.read(_MAX_DOWNLOAD_BYTES + 1)
    except OSError as error:
        raise IndexTransportError("artifact download failed") from error
    if not isinstance(payload, bytes) or len(payload) > _MAX_DOWNLOAD_BYTES:
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
        parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("index response is invalid JSON") from error
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
    if not isinstance(payload, bytes) or len(payload) > _MAX_DOWNLOAD_BYTES:
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
        for artifact in downloaded:
            artifact.path.unlink(missing_ok=True)
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
