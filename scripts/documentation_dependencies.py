"""Prepare original hashed wheels without executing candidate project code.

This closure is preparatory evidence. It grants no release or tool approval.
Only frozen metadata enters export children; pip runs under the selected target
interpreter with site startup disabled and an independent distro module root.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath

from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process, run_bounded_process_bytes
from scripts import _release_bundle_evidence, release_filesystem, release_wheelhouse
from scripts.documentation_ca import system_ca_mounts
from scripts.documentation_runtime import DependencyInput, DependencyTarget, RuntimeTools
from scripts.json_policy import load_json_bytes
from scripts.release_consumer import validate_hashed_requirements

_METADATA_LIMIT = 8 * 1024 * 1024
_REQUIREMENTS_LIMIT = 4 * 1024 * 1024
_MANIFEST_LIMIT = 2 * 1024 * 1024
_OUTPUT_LIMIT = 64 * 1024
_ENVELOPE_LIMIT = _release_bundle_evidence.MAX_WHEELHOUSE_BYTES + _MANIFEST_LIMIT
_PREPARATION_TIMEOUT_SECONDS = 300
_CLEANUP_TIMEOUT_SECONDS = 3
_MANIFEST = "preparation-manifest.json"
_REQUIREMENTS = ("runtime-requirements.txt", "release-build-requirements.txt")
_AUTHORITY = "preparatory-only; no approval granted"
_TARGET_PROBE = (
    "import json,sys,sysconfig; "
    "print(json.dumps({'version':list(sys.version_info[:3]),"
    "'implementation':sys.implementation.name,'cache_tag':sys.implementation.cache_tag,"
    "'platform':sysconfig.get_platform()}))"
)
_HOST_PROBE = "import sys; print('%s.%s' % sys.version_info[:2])"
_PIP_ENTRY = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('pip',run_name='__main__')"


def _read_at(parent: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, release_filesystem.file_flags() | os.O_CLOEXEC, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        data = release_filesystem.read_regular_file(descriptor, maximum_bytes=limit)
        after = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in identity) or len(data) != before.st_size:
            raise ValueError("dependency input changed during capture")
        return data
    finally:
        os.close(descriptor)


def _metadata(root: Path) -> dict[str, bytes]:
    descriptor = release_filesystem.open_real_directory(root)
    try:
        return {name: _read_at(descriptor, name, _METADATA_LIMIT) for name in ("pyproject.toml", "uv.lock")}
    finally:
        os.close(descriptor)


def _environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_NO_CACHE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_PROGRESS": "1",
    }


def _run(argv: list[str], descriptors: tuple[int, ...] = (), *, limit: int = _OUTPUT_LIMIT) -> str:
    # Bubblewrap consumes ro-bind-data streams; repeated children need the same
    # captured bytes, not the shared descriptor's previous end-of-file offset.
    for descriptor in descriptors:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        result = run_bounded_process(
            argv,
            timeout=_PREPARATION_TIMEOUT_SECONDS,
            stdout_limit=limit,
            stderr_limit=_OUTPUT_LIMIT,
            cleanup_timeout=_CLEANUP_TIMEOUT_SECONDS,
            env=_environment(),
            pass_fds=descriptors,
        )
    except (BoundedProcessError, OSError) as error:
        raise ValueError("dependency preparation command could not complete within its bounds") from error
    if result.returncode != 0:
        raise ValueError(f"dependency preparation command failed with exit {result.returncode}: {result.stderr[:4000]}")
    return result.stdout


def _pip_root() -> Path:
    version = _run(["/usr/bin/python3", "-I", "-S", "-c", _HOST_PROBE]).strip()
    if re.fullmatch(r"3\.[0-9]+", version) is None:
        raise ValueError("independent distro Python version is unavailable")
    candidates = [Path(base) / f"python{version}" / "site-packages" for base in ("/usr/lib", "/usr/lib64")]
    available = [root for root in candidates if (root / "pip" / "__init__.py").is_file()]
    if len(available) != 1:
        raise ValueError("install distro python3-pip: exactly one fixed system pip module root is required")
    descriptor = release_filesystem.open_real_directory(available[0] / "pip")
    try:
        _read_at(descriptor, "__init__.py", _METADATA_LIMIT)
    finally:
        os.close(descriptor)
    return available[0]


@contextmanager
def _sandbox(
    scratch: Path, tools: RuntimeTools, *, network: bool = False
) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    descriptors: list[int] = []
    try:
        usr = release_filesystem.open_real_directory(Path("/usr"))
        descriptors.append(usr)
        scratch_fd = release_filesystem.open_real_directory(scratch)
        descriptors.append(scratch_fd)
        descriptors.append(os.dup(tools.python_descriptor))
        descriptors.append(os.dup(tools.uv_descriptor))
        command = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--unshare-user",
            "--die-with-parent",
            "--new-session",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind-fd",
            str(usr),
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--dir",
            "/run",
            "--ro-bind-fd",
            str(descriptors[-2]),
            "/run/python",
            "--ro-bind-fd",
            str(descriptors[-1]),
            "/run/uv",
            "--ro-bind-fd" if network else "--bind-fd",
            str(scratch_fd),
            "/workspace",
            "--dir",
            "/home",
            "--dir",
            "/home/linuxbrew",
            "--dir",
            "/home/linuxbrew/.linuxbrew",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--dir",
            "/home/linuxbrew/.linuxbrew/lib",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--symlink",
            "/lib64/ld-linux-x86-64.so.2",
            "/home/linuxbrew/.linuxbrew/lib/ld.so",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--dir",
            "/etc",
        ]
        if network:
            command.extend(
                ("--share-net", "--size", str(_release_bundle_evidence.MAX_WHEELHOUSE_BYTES), "--tmpfs", "/wheels")
            )
            for name in ("resolv.conf", "hosts", "nsswitch.conf"):
                path = Path("/etc") / name
                # Distro resolver aliases are resolved before the pinned regular read.
                resolved = path.resolve(strict=True)
                parent = release_filesystem.open_real_directory(resolved.parent)
                try:
                    data = _read_at(parent, resolved.name, _OUTPUT_LIMIT)
                finally:
                    os.close(parent)
                snapshot = scratch / f"resolver-{name}"
                snapshot.write_bytes(data)
                fd = os.open(snapshot, release_filesystem.file_flags())
                descriptors.append(fd)
                snapshot.unlink()
                command.extend(("--ro-bind-data", str(fd), str(path)))
        command.extend(system_ca_mounts(scratch, descriptors))
        command.extend(("--chdir", "/workspace", "--"))
        yield command, tuple(descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _export(scratch: Path, tools: RuntimeTools) -> tuple[DependencyTarget, dict[str, bytes]]:
    with _sandbox(scratch, tools) as (sandbox, descriptors):
        target = load_json_bytes(_run([*sandbox, "/run/python", "-I", "-S", "-c", _TARGET_PROBE], descriptors).encode())
        if not isinstance(target, dict) or set(target) != {"version", "implementation", "cache_tag", "platform"}:
            raise ValueError("dependency target interpreter probe is invalid")
        version, implementation = target["version"], target["implementation"]
        cache_tag, platform = target["cache_tag"], target["platform"]
        if (
            not isinstance(version, list)
            or len(version) != 3
            or not all(isinstance(part, int) and not isinstance(part, bool) for part in version)
            or not isinstance(implementation, str)
            or not isinstance(platform, str)
            or not (cache_tag is None or isinstance(cache_tag, str))
        ):
            raise ValueError("dependency target interpreter probe is invalid")
        target_identity: DependencyTarget = {
            "version": version,
            "implementation": implementation,
            "cache_tag": cache_tag,
            "platform": platform,
        }
        requirements: dict[str, bytes] = {}
        for name, group in zip(_REQUIREMENTS, ((), ("--only-group", "release-build")), strict=True):
            output = _run(
                [
                    *sandbox,
                    "/run/uv",
                    "--no-config",
                    "export",
                    "--offline",
                    "--locked",
                    "--no-default-groups",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-header",
                    "--no-annotate",
                    "--python",
                    "/run/python",
                    *group,
                ],
                descriptors,
                limit=_REQUIREMENTS_LIMIT,
            )
            path = scratch / name
            path.write_text(output, encoding="utf-8")
            validate_hashed_requirements(path)
            requirements[name] = output.encode("utf-8")
        return target_identity, requirements


_DOWNLOAD_BOOTSTRAP = """
import contextlib, os, pathlib, sys, zipfile
os.environ['TMPDIR'] = '/wheels'
sys.path.insert(0, sys.argv[1])
from pip._internal.cli.main import main
with contextlib.redirect_stdout(sys.stderr):
    for name in ('runtime-requirements.txt', 'release-build-requirements.txt'):
        status = main(['--isolated', '--disable-pip-version-check', 'download',
            '--no-cache-dir', '--index-url', 'https://pypi.org/simple',
            '--require-hashes', '--only-binary=:all:', '--no-deps',
            '--timeout', '30', '--retries', '0', '--dest', '/wheels',
            '--requirement', '/workspace/' + name])
        if status:
            raise SystemExit(status)
files = sorted(pathlib.Path('/wheels').iterdir())
if len(files) > int(sys.argv[2]):
    raise ValueError('download wheel count exceeds limit')
with zipfile.ZipFile(sys.stdout.buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
    for path in files:
        if not path.is_file() or path.is_symlink() or path.suffix != '.whl':
            raise ValueError('download returned an unsafe entry')
        archive.write(path, path.name)
"""


def _download_envelope(scratch: Path, tools: RuntimeTools, pip_root: Path) -> bytes:
    with _sandbox(scratch, tools, network=True) as (sandbox, descriptors):
        try:
            result = run_bounded_process_bytes(
                [
                    *sandbox,
                    "/run/python",
                    "-I",
                    "-S",
                    "-c",
                    _DOWNLOAD_BOOTSTRAP,
                    str(pip_root),
                    str(release_wheelhouse.MAX_WHEELHOUSE_MEMBERS),
                ],
                timeout=_PREPARATION_TIMEOUT_SECONDS,
                stdout_limit=_ENVELOPE_LIMIT,
                stderr_limit=_OUTPUT_LIMIT,
                cleanup_timeout=_CLEANUP_TIMEOUT_SECONDS,
                env=_environment(),
                pass_fds=descriptors,
            )
        except (BoundedProcessError, OSError) as error:
            raise ValueError("dependency wheel download exceeded its bounds or could not start") from error
        if result.returncode != 0:
            raise ValueError(
                f"dependency wheel download failed with exit {result.returncode}: "
                f"{result.stderr[:4000].decode('utf-8', errors='replace')}"
            )
        return result.stdout


def _decode_envelope(data: bytes, requirements: dict[str, bytes]) -> dict[str, bytes]:
    if len(data) > _ENVELOPE_LIMIT:
        raise ValueError("download envelope exceeds size limit")
    hashes = {
        token.removeprefix(b"--hash=sha256:").decode("ascii")
        for content in requirements.values()
        for token in content.split()
        if token.startswith(b"--hash=sha256:")
    }
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > release_wheelhouse.MAX_WHEELHOUSE_MEMBERS:
                raise ValueError("download envelope requires bounded original wheel entries")
            total = 0
            wheels: dict[str, bytes] = {}
            for member in members:
                mode = member.external_attr >> 16
                if (
                    re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", member.filename) is None
                    or member.filename in wheels
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or member.compress_type != zipfile.ZIP_STORED
                ):
                    raise ValueError("download envelope contains unsafe entries")
                total += member.file_size
                if total > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
                    raise ValueError("download envelope wheel bytes exceed size limit")
                content = archive.read(member)
                if sha256(content).hexdigest() not in hashes:
                    raise ValueError("downloaded original wheel does not match frozen requirement hashes")
                _wheel(content)
                wheels[member.filename] = content
            return wheels
    except zipfile.BadZipFile as error:
        raise ValueError("download envelope or original wheel ZIP is invalid") from error


def _acquire(scratch: Path, tools: RuntimeTools, pip_root: Path) -> None:
    requirements = {name: (scratch / name).read_bytes() for name in _REQUIREMENTS}
    wheels = _decode_envelope(_download_envelope(scratch, tools, pip_root), requirements)
    (scratch / "wheels").mkdir()
    for name, content in wheels.items():
        (scratch / "wheels" / name).write_bytes(content)


def _verify_target(scratch: Path, tools: RuntimeTools, pip_root: Path) -> None:
    """Resolve target markers and wheel compatibility offline without installing."""
    (scratch / "verified-wheels").mkdir()
    with _sandbox(scratch, tools) as (sandbox, descriptors):
        for name in _REQUIREMENTS:
            _run(
                [
                    *sandbox,
                    "/run/python",
                    "-I",
                    "-S",
                    "-c",
                    _PIP_ENTRY,
                    str(pip_root),
                    "--isolated",
                    "--disable-pip-version-check",
                    "download",
                    "--no-cache-dir",
                    "--no-index",
                    "--find-links",
                    "/workspace/wheels",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--no-deps",
                    "--dest",
                    "/workspace/verified-wheels",
                    "--requirement",
                    f"/workspace/{name}",
                ],
                descriptors,
            )


def _wheel(data: bytes) -> None:
    try:
        _validate_wheel(data)
    except zipfile.BadZipFile as error:
        raise ValueError("original wheel ZIP is invalid") from error


def _validate_wheel(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > release_wheelhouse.MAX_WHEELHOUSE_MEMBERS:
            raise ValueError("original wheel contains too many members")
        names: set[str] = set()
        total = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (
                not entry.filename
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or entry.filename in names
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
            ):
                raise ValueError("original wheel contains unsafe members")
            names.add(entry.filename)
            total += entry.file_size
            if total > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
                raise ValueError("original wheel expanded contents exceed the size limit")


def _capture_wheels(descriptor: int, requirements: dict[str, bytes]) -> dict[str, bytes]:
    wheels_fd = release_filesystem.open_directory_at(descriptor, "wheels")
    try:
        names = sorted(os.listdir(wheels_fd))  # noqa: PTH208 - enumerate the pinned directory, not its path
        if not names or len(names) > release_wheelhouse.MAX_WHEELHOUSE_MEMBERS:
            raise ValueError("dependency closure requires a bounded nonempty wheelhouse")
        permitted_hashes = {
            token.removeprefix(b"--hash=sha256:").decode("ascii")
            for data in requirements.values()
            for token in data.split()
            if token.startswith(b"--hash=sha256:")
        }
        wheels: dict[str, bytes] = {}
        total = 0
        for name in names:
            if re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", name) is None:
                raise ValueError("dependency closure wheelhouse contains an unsafe entry")
            data = _read_at(wheels_fd, name, _release_bundle_evidence.MAX_WHEELHOUSE_BYTES)
            total += len(data)
            if total > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
                raise ValueError("dependency closure wheel bytes exceed the size limit")
            if sha256(data).hexdigest() not in permitted_hashes:
                raise ValueError("original wheel does not match frozen requirement hashes")
            _wheel(data)
            wheels[name] = data
        return wheels
    finally:
        os.close(wheels_fd)


def _manifest(
    metadata: dict[str, bytes], target: DependencyTarget, requirements: dict[str, bytes], wheels: dict[str, bytes]
) -> bytes:
    value = {
        "schema": 1,
        "authority": _AUTHORITY,
        "target": target,
        "source": {name: sha256(data).hexdigest() for name, data in metadata.items()},
        "requirements": {name: sha256(data).hexdigest() for name, data in requirements.items()},
        "wheels": [
            {"name": name, "size": len(data), "sha256": sha256(data).hexdigest()}
            for name, data in sorted(wheels.items())
        ],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _supplied(
    source: DependencyInput, metadata: dict[str, bytes], target: DependencyTarget, requirements: dict[str, bytes]
) -> tuple[bytes, dict[str, bytes]]:
    descriptor = source.directory_descriptor
    if set(os.listdir(descriptor)) != {_MANIFEST, "wheels", *_REQUIREMENTS}:  # noqa: PTH208 - held directory
        raise ValueError("supplied dependency closure contains unexpected entries")
    manifest = _read_at(descriptor, _MANIFEST, _MANIFEST_LIMIT)
    if sha256(manifest).hexdigest() != source.manifest_sha256:
        raise ValueError("supplied dependency manifest digest mismatch")
    for name, expected in requirements.items():
        if _read_at(descriptor, name, _REQUIREMENTS_LIMIT) != expected:
            raise ValueError("supplied dependency requirements differ from frozen metadata export")
    wheels = _capture_wheels(descriptor, requirements)
    if json.dumps(load_json_bytes(manifest), sort_keys=True) != json.dumps(
        load_json_bytes(_manifest(metadata, target, requirements, wheels)), sort_keys=True
    ):
        raise ValueError("supplied dependency manifest does not bind this source, target, and original wheels")
    return manifest, wheels


def capture_prepared_input(
    root: Path, manifest_sha256: str, metadata_root: Path, target: DependencyTarget
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Capture subject bytes bound to an independently retained preparation digest.

    This consumer check grants no approval and performs no network acquisition.
    Preparation, including independent lock export, precedes retaining the digest.
    """
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("dependency manifest digest is invalid")
    descriptor = release_filesystem.open_real_directory(root)
    try:
        requirements = {name: _read_at(descriptor, name, _REQUIREMENTS_LIMIT) for name in _REQUIREMENTS}
        with tempfile.TemporaryDirectory(prefix="fieldkit-dependency-input-") as directory:
            for name, data in requirements.items():
                path = Path(directory) / name
                path.write_bytes(data)
                validate_hashed_requirements(path)
        _, wheels = _supplied(
            DependencyInput(descriptor, manifest_sha256), _metadata(metadata_root), target, requirements
        )
        return requirements, wheels
    finally:
        os.close(descriptor)


@contextmanager
def prepare_dependencies(
    metadata_root: Path, tools: RuntimeTools, *, supplied: DependencyInput | None = None
) -> Iterator[DependencyInput]:
    """Export frozen pins and hold a verified private original-wheel snapshot.

    A supplied input is revalidated against an independent locked export, not
    trusted merely because its manifest contains matching source digests.
    Missing tools or bytes fail without an online installer fallback.
    """
    metadata = _metadata(metadata_root)
    with tempfile.TemporaryDirectory(prefix="fieldkit-dependency-preparation-") as directory:
        scratch = Path(directory) / "metadata"
        scratch.mkdir()
        for name, data in metadata.items():
            (scratch / name).write_bytes(data)
        target, requirements = _export(scratch, tools)
        pip_root = _pip_root()
        if supplied is None:
            _acquire(scratch, tools, pip_root)
            source_fd = release_filesystem.open_real_directory(scratch)
            try:
                wheels = _capture_wheels(source_fd, requirements)
            finally:
                os.close(source_fd)
            manifest = _manifest(metadata, target, requirements, wheels)
        else:
            manifest, wheels = _supplied(supplied, metadata, target, requirements)
            (scratch / "wheels").mkdir()
            for name, data in wheels.items():
                (scratch / "wheels" / name).write_bytes(data)
        _verify_target(scratch, tools, pip_root)
        closure = Path(directory) / "closure"
        closure.mkdir()
        (closure / "wheels").mkdir()
        for name, data in requirements.items():
            (closure / name).write_bytes(data)
        for name, data in wheels.items():
            (closure / "wheels" / name).write_bytes(data)
        (closure / _MANIFEST).write_bytes(manifest)
        descriptor = release_filesystem.open_real_directory(closure)
        try:
            yield DependencyInput(descriptor, sha256(manifest).hexdigest())
        finally:
            os.close(descriptor)
