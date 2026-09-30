"""Frozen-pin preparation, supplied closure integrity, and startup exclusion."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import documentation_dependencies as dependencies
from scripts.documentation_runtime import DependencyInput, DependencyTarget, RuntimeTools
from scripts.release_consumer import validate_hashed_requirements

pytestmark = pytest.mark.unit
_TARGET: DependencyTarget = {
    "version": [3, 11, 16],
    "implementation": "cpython",
    "cache_tag": "cpython-311",
    "platform": "linux-x86_64",
}


def _wheel_bytes(member: str = "demo/__init__.py", content: bytes = b"raise RuntimeError('must not import')") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, content)
    return stream.getvalue()


@pytest.fixture
def preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, RuntimeTools, dict[str, bytes]]:
    metadata = tmp_path / "source"
    metadata.mkdir()
    (metadata / "pyproject.toml").write_text("[project]\nname='demo'\nversion='1.0'\n", encoding="utf-8")
    (metadata / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    wheel = _wheel_bytes("sentinel.pth", b"import pathlib; pathlib.Path('/tmp/must-not-execute').touch()")
    requirement = f"demo==1.0 --hash=sha256:{sha256(wheel).hexdigest()}\n".encode()
    requirements: dict[str, bytes] = dict.fromkeys(dependencies._REQUIREMENTS, requirement)

    def export(scratch: Path, tools: RuntimeTools) -> tuple[DependencyTarget, dict[str, bytes]]:
        assert {path.name for path in scratch.iterdir()} == {"pyproject.toml", "uv.lock"}
        for name, data in requirements.items():
            (scratch / name).write_bytes(data)
            validate_hashed_requirements(scratch / name)
        return _TARGET, requirements

    def acquire(scratch: Path, tools: RuntimeTools, pip_root: Path) -> None:
        (scratch / "wheels").mkdir()
        (scratch / "wheels" / "demo-1.0-py3-none-any.whl").write_bytes(wheel)

    monkeypatch.setattr(dependencies, "_export", export)
    monkeypatch.setattr(dependencies, "_acquire", acquire)
    monkeypatch.setattr(dependencies, "_pip_root", lambda: Path("/usr/lib/python3.14/site-packages"))
    monkeypatch.setattr(dependencies, "_verify_target", lambda scratch, tools, pip_root: None)
    return metadata, RuntimeTools(-1, -1), requirements


def test_preparation_holds_private_original_snapshot_and_reuse_reexports(
    preparation: tuple[Path, RuntimeTools, dict[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata, tools, requirements = preparation
    with dependencies.prepare_dependencies(metadata, tools) as original:
        assert isinstance(original, DependencyInput)
        manifest = dependencies._read_at(original.directory_descriptor, dependencies._MANIFEST, 100_000)
        assert original.manifest_sha256 == sha256(manifest).hexdigest()
        values = dependencies.load_json_bytes(manifest)
        assert isinstance(values, dict) and values["authority"] == dependencies._AUTHORITY
        wheels = dependencies._capture_wheels(original.directory_descriptor, requirements)
        assert list(wheels) == ["demo-1.0-py3-none-any.whl"]
        assert b"must-not-execute" in next(iter(wheels.values()))
        monkeypatch.setattr(dependencies, "_acquire", lambda *args: pytest.fail("supplied closure must stay offline"))
        with dependencies.prepare_dependencies(metadata, tools, supplied=original) as reused:
            assert reused.directory_descriptor != original.directory_descriptor
            assert reused.manifest_sha256 == original.manifest_sha256
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(reused.directory_descriptor)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(original.directory_descriptor)


@pytest.mark.parametrize("mutation", ["manifest", "requirement", "wheel", "extra", "target", "source"])
def test_supplied_input_tampering_fails(
    preparation: tuple[Path, RuntimeTools, dict[str, bytes]], mutation: str
) -> None:
    metadata, tools, _ = preparation
    with dependencies.prepare_dependencies(metadata, tools) as original:
        source = original
        root = Path(f"/proc/self/fd/{original.directory_descriptor}")
        if mutation == "manifest":
            (root / dependencies._MANIFEST).write_bytes(b"{}")
        elif mutation == "requirement":
            (root / dependencies._REQUIREMENTS[0]).write_bytes(b"arbitrary==9.0\n")
        elif mutation == "wheel":
            (root / "wheels" / "demo-1.0-py3-none-any.whl").write_bytes(b"changed")
        elif mutation == "extra":
            (root / "unexpected").write_bytes(b"untrusted")
        elif mutation == "target":
            data = dependencies.load_json_bytes((root / dependencies._MANIFEST).read_bytes())
            assert isinstance(data, dict)
            data["target"] = {"version": [3, 14, 7]}
            (root / dependencies._MANIFEST).write_text(dependencies.json.dumps(data), encoding="utf-8")
            source = DependencyInput(
                original.directory_descriptor, sha256((root / dependencies._MANIFEST).read_bytes()).hexdigest()
            )
        else:
            (metadata / "uv.lock").write_text("version=2\n", encoding="utf-8")
        with (
            pytest.raises(ValueError, match=r"digest|frozen|unexpected|bind"),
            dependencies.prepare_dependencies(metadata, tools, supplied=source),
        ):
            pytest.fail("tampered closure must not be yielded")


def test_self_rehashed_foreign_requirement_is_not_accepted(
    preparation: tuple[Path, RuntimeTools, dict[str, bytes]],
) -> None:
    metadata, tools, _ = preparation
    with dependencies.prepare_dependencies(metadata, tools) as original:
        root = Path(f"/proc/self/fd/{original.directory_descriptor}")
        altered = b"other==1.0 --hash=sha256:" + b"a" * 64 + b"\n"
        (root / dependencies._REQUIREMENTS[0]).write_bytes(altered)
        manifest = dependencies.load_json_bytes((root / dependencies._MANIFEST).read_bytes())
        assert isinstance(manifest, dict)
        declared = manifest["requirements"]
        assert isinstance(declared, dict)
        declared[dependencies._REQUIREMENTS[0]] = sha256(altered).hexdigest()
        data = dependencies.json.dumps(manifest).encode()
        (root / dependencies._MANIFEST).write_bytes(data)
        source = DependencyInput(original.directory_descriptor, sha256(data).hexdigest())
        with (
            pytest.raises(ValueError, match="frozen metadata export"),
            dependencies.prepare_dependencies(metadata, tools, supplied=source),
        ):
            pytest.fail("caller-authenticated manifest must not override frozen pins")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversized"])
def test_metadata_rejects_unsafe_files(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "pyproject.toml"
    if kind == "symlink":
        other = tmp_path / "other"
        other.write_bytes(b"secret")
        target.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    else:
        with target.open("wb") as stream:
            stream.truncate(dependencies._METADATA_LIMIT + 1)
    with pytest.raises((OSError, ValueError), match=r"symbolic|regular|size"):
        dependencies._metadata(tmp_path)


@pytest.mark.parametrize("member", ["../escape.py", "/absolute.py", "nested\\escape.py"])
def test_original_wheel_rejects_unsafe_member_names(member: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        dependencies._wheel(_wheel_bytes(member))


def test_original_wheel_rejects_symlink_and_duplicate() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        entry = zipfile.ZipInfo("symlink")
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, "outside")
    with pytest.raises(ValueError, match="unsafe"):
        dependencies._wheel(stream.getvalue())
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("same", "one")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("same", "two")
    with pytest.raises(ValueError, match="unsafe"):
        dependencies._wheel(stream.getvalue())


def test_selected_interpreter_startup_does_not_import_candidate_site(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    sentinel = tmp_path / "executed"
    (site / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(sentinel)!r}).touch()", encoding="utf-8"
    )
    (site / "sentinel.pth").write_text(f"import pathlib; pathlib.Path({str(sentinel)!r}).touch()", encoding="utf-8")
    pip = tmp_path / "independent" / "pip"
    pip.mkdir(parents=True)
    (pip / "__init__.py").write_text("", encoding="utf-8")
    (pip / "__main__.py").write_text("import sys; print(sys.flags.isolated, sys.flags.no_site)", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", dependencies._PIP_ENTRY, str(pip.parent)],
        cwd=site,
        env={"PYTHONPATH": str(site), "HOME": str(site)},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "1 1"
    assert not sentinel.exists()


def test_environment_ignores_credentials_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://secret.invalid")
    monkeypatch.setenv("PYTHONPATH", "/candidate/site-packages")
    monkeypatch.setenv("UV_INDEX", "https://secret.invalid")
    environment = dependencies._environment()
    assert environment["HOME"] == "/tmp"
    assert not {"PIP_EXTRA_INDEX_URL", "PYTHONPATH", "UV_INDEX"} & environment.keys()
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"


def test_download_and_offline_check_use_actual_target_without_version_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def sandbox(
        scratch: Path, tools: RuntimeTools, *, network: bool = False
    ) -> Iterator[tuple[list[str], tuple[int, ...]]]:
        assert scratch == tmp_path
        yield ["sandbox", "network" if network else "offline", "--"], ()

    commands: list[list[str]] = []

    def run(argv: list[str], descriptors: tuple[int, ...] = (), *, limit: int = 1000) -> str:
        commands.append(argv)
        return ""

    monkeypatch.setattr(dependencies, "_sandbox", sandbox)
    monkeypatch.setattr(dependencies, "_run", run)
    tools = RuntimeTools(-1, -1)
    pip_root = Path("/usr/lib/python3.14/site-packages")
    dependencies._verify_target(tmp_path, tools, pip_root)
    assert len(commands) == 2
    for argv in commands:
        assert argv[argv.index("/run/python") + 1 : argv.index("-c")] == ["-I", "-S"]
        assert "--python-version" not in argv
        assert str(pip_root) in argv
        assert {"--require-hashes", "--no-deps", "--only-binary=:all:", "--no-cache-dir"} <= set(argv)
        assert "install" not in argv
    assert "--no-index" in commands[0] and "offline" in commands[0]
    assert "https://pypi.org/simple" in dependencies._DOWNLOAD_BOOTSTRAP
    assert "os.environ['TMPDIR'] = '/wheels'" in dependencies._DOWNLOAD_BOOTSTRAP


def test_sandbox_closes_first_duplicate_when_second_duplicate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dup = os.dup
    duplicated: list[int] = []

    def duplicate(descriptor: int) -> int:
        if duplicated:
            raise OSError("second duplicate unavailable")
        result = real_dup(descriptor)
        duplicated.append(result)
        return result

    descriptor = os.open(sys.executable, os.O_RDONLY)
    monkeypatch.setattr(dependencies.os, "dup", duplicate)
    try:
        with (
            pytest.raises(OSError, match="second duplicate"),
            dependencies._sandbox(tmp_path, RuntimeTools(descriptor, descriptor)),
        ):
            pytest.fail("failed mount preparation must not yield")
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(duplicated[0])
    finally:
        os.close(descriptor)


def _envelope(name: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(name, data)
    return output.getvalue()


def test_download_envelope_preserves_original_bytes_and_hashes() -> None:
    wheel = _wheel_bytes()
    requirement = f"demo==1.0 --hash=sha256:{sha256(wheel).hexdigest()}\n".encode()
    result = dependencies._decode_envelope(_envelope("demo-1.0-py3-none-any.whl", wheel), {"runtime": requirement})
    assert result == {"demo-1.0-py3-none-any.whl": wheel}


@pytest.mark.parametrize("kind", ["traversal", "wrong-hash", "malformed", "duplicate", "symlink", "aggregate"])
def test_download_envelope_rejects_unsafe_or_overbound_inputs(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = _wheel_bytes()
    requirement = f"demo==1.0 --hash=sha256:{sha256(wheel).hexdigest()}\n".encode()
    if kind == "malformed":
        envelope = b"not ZIP"
    elif kind == "traversal":
        envelope = _envelope("../demo-1.0-py3-none-any.whl", wheel)
    elif kind == "wrong-hash":
        envelope = _envelope("demo-1.0-py3-none-any.whl", b"tampered")
    elif kind == "aggregate":
        envelope = _envelope("demo-1.0-py3-none-any.whl", wheel)
        monkeypatch.setattr(dependencies._release_bundle_evidence, "MAX_WHEELHOUSE_BYTES", len(wheel) - 1)
    else:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            if kind == "symlink":
                entry = zipfile.ZipInfo("demo-1.0-py3-none-any.whl")
                entry.external_attr = 0o120777 << 16
                archive.writestr(entry, wheel)
            else:
                archive.writestr("demo-1.0-py3-none-any.whl", wheel)
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr("demo-1.0-py3-none-any.whl", wheel)
        envelope = stream.getvalue()
    with pytest.raises(ValueError, match=r"unsafe|hashes|invalid|size"):
        dependencies._decode_envelope(envelope, {"runtime": requirement})


def test_network_sandbox_uses_readonly_metadata_and_capped_transient_storage(tmp_path: Path) -> None:
    descriptor = os.open(sys.executable, os.O_RDONLY)
    try:
        with dependencies._sandbox(tmp_path, RuntimeTools(descriptor, descriptor), network=True) as (command, held):
            assert command[command.index("--size") + 1 : command.index("--size") + 4] == [
                str(dependencies._release_bundle_evidence.MAX_WHEELHOUSE_BYTES),
                "--tmpfs",
                "/wheels",
            ]
            workspace = command.index("/workspace")
            assert command[workspace - 2] == "--ro-bind-fd"
            assert "--share-net" in command
            assert all(os.fstat(fd).st_ino > 0 for fd in held)
        for fd in held:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
    finally:
        os.close(descriptor)


def test_failed_target_closure_is_not_yielded_or_reacquired_online(
    preparation: tuple[Path, RuntimeTools, dict[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata, tools, _ = preparation
    with dependencies.prepare_dependencies(metadata, tools) as supplied:
        monkeypatch.setattr(
            dependencies, "_acquire", lambda *args: pytest.fail("offline input must not acquire online")
        )

        def missing(scratch: Path, selected: RuntimeTools, pip_root: Path) -> None:
            raise ValueError("offline target dependency is missing")

        monkeypatch.setattr(dependencies, "_verify_target", missing)
        with (
            pytest.raises(ValueError, match="target dependency is missing"),
            dependencies.prepare_dependencies(metadata, tools, supplied=supplied),
        ):
            pytest.fail("incomplete target closure must not be yielded")


def test_boolean_schema_does_not_equal_numeric_schema(preparation: tuple[Path, RuntimeTools, dict[str, bytes]]) -> None:
    metadata, tools, _ = preparation
    with dependencies.prepare_dependencies(metadata, tools) as supplied:
        root = Path(f"/proc/self/fd/{supplied.directory_descriptor}")
        manifest = dependencies.load_json_bytes((root / dependencies._MANIFEST).read_bytes())
        assert isinstance(manifest, dict)
        manifest["schema"] = True
        data = dependencies.json.dumps(manifest).encode()
        (root / dependencies._MANIFEST).write_bytes(data)
        changed = DependencyInput(supplied.directory_descriptor, sha256(data).hexdigest())
        with (
            pytest.raises(ValueError, match="does not bind"),
            dependencies.prepare_dependencies(metadata, tools, supplied=changed),
        ):
            pytest.fail("boolean schema must not match numeric schema")


def test_consumer_capture_checks_retained_digest_and_returns_original_bytes(
    preparation: tuple[Path, RuntimeTools, dict[str, bytes]],
) -> None:
    metadata, tools, requirements = preparation
    with dependencies.prepare_dependencies(metadata, tools) as original:
        root = Path(f"/proc/self/fd/{original.directory_descriptor}").resolve()
        result = dependencies.capture_prepared_input(root, original.manifest_sha256, metadata, _TARGET)
        assert result[0] == requirements
        assert list(result[1]) == ["demo-1.0-py3-none-any.whl"]
        with pytest.raises(ValueError, match="digest mismatch"):
            dependencies.capture_prepared_input(root, "0" * 64, metadata, _TARGET)
        (metadata / "uv.lock").write_text("changed", encoding="utf-8")
        with pytest.raises(ValueError, match="does not bind"):
            dependencies.capture_prepared_input(root, original.manifest_sha256, metadata, _TARGET)


def test_preparation_sandbox_explicitly_unshares_user_namespace(tmp_path: Path) -> None:
    descriptor = os.open(sys.executable, os.O_RDONLY)
    try:
        with dependencies._sandbox(tmp_path, RuntimeTools(descriptor, descriptor)) as (command, _):
            assert "--unshare-user" in command
            assert "--disable-userns" in command
    finally:
        os.close(descriptor)


@pytest.mark.integration
def test_offline_preparation_system_pip_can_load_its_ca_bundle(tmp_path: Path) -> None:
    pip_root = dependencies._pip_root()
    uv_path = shutil.which("uv")
    assert uv_path is not None
    uv = os.open(uv_path, os.O_RDONLY)
    python = os.open("/proc/self/exe", os.O_RDONLY)
    try:
        with dependencies._sandbox(tmp_path, RuntimeTools(uv, python)) as (sandbox, descriptors):
            result = dependencies._run(
                [
                    *sandbox,
                    "/run/python",
                    "-I",
                    "-S",
                    "-c",
                    "import sys; sys.path.insert(0,sys.argv[1]); import pip._vendor.requests; print('loaded')",
                    str(pip_root),
                ],
                descriptors,
            )
            assert result == "loaded\n"
            repeated = dependencies._run(
                [
                    *sandbox,
                    "/run/python",
                    "-I",
                    "-S",
                    "-c",
                    "import sys; sys.path.insert(0,sys.argv[1]); import pip._vendor.requests; print('loaded')",
                    str(pip_root),
                ],
                descriptors,
            )
            assert repeated == "loaded\n"
    finally:
        os.close(python)
        os.close(uv)
