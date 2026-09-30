"""Synthetic independently injected pins exercise byte capture, not release approval."""

from __future__ import annotations

import hashlib
import io
import os
import resource
import socket
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import release_controller_bootstrap as bootstrap
from scripts import release_controller_capture_worker as worker
from scripts import release_controller_execution_input as execution
from scripts import release_controller_runtime_manifest as manifest
from scripts.release_controller_elf import ElfMetadata, validate_elf_graph
from scripts.release_controller_snapshot import FileExpectation, SnapshotBudget, capture_stream
from tests.test_release_controller_runtime_manifest import encoded, runtime_fixture
from tests.test_release_trust_contracts import fixture_selection, mapping

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 30, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


class SyntheticAuthority(execution.TrustedExecutionAuthority):
    def __init__(self, state: execution.ExecutionAuthorityState) -> None:
        self.state = state
        self.calls = 0

    def current_state(self) -> execution.ExecutionAuthorityState:
        self.calls += 1
        return self.state


@dataclass(frozen=True)
class Fixture:
    selection: bootstrap.SelectionBoundControllerCapture
    raw_manifest: bytes
    runtime: Path
    archive: Path
    authority: SyntheticAuthority


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def candidate(tmp_path: Path) -> Fixture:
    raw_manifest, contents = runtime_fixture()
    runtime = tmp_path / "runtime"
    for name, data in contents.items():
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest_bytes = encoded(raw_manifest)
    schema_root = ROOT / "docs/release-readiness"
    deployment = bootstrap.BootstrapDeploymentPolicy(
        (schema_root / "release-trust-selection.schema.json").read_bytes(),
        (schema_root / "release-controller-receipt.schema.json").read_bytes(),
        frozenset({7}),
    )
    controller = tmp_path / "controller"
    (controller / "scripts").mkdir(parents=True)
    sources = {
        "scripts/__init__.py": b'raise RuntimeError("selected sentinel")\n',
        "scripts/main.py": b'raise RuntimeError("selected sentinel")\n',
    }
    for name, data in sources.items():
        (controller / name).write_bytes(data)
    c0_manifest = encoded(
        {
            "schema_version": 1,
            "kind": "fieldkit.release-controller-manifest",
            "entrypoint": "scripts/main.py",
            "members": [
                {"path": name, "size_bytes": len(data), "sha256": checksum(data)}
                for name, data in sorted(sources.items())
            ],
        }
    )
    selected = fixture_selection()
    mapping(selected["policy"]).update(
        selection_schema_sha256=checksum(deployment.selection_schema_bytes),
        receipt_schema_sha256=checksum(deployment.receipt_schema_bytes),
    )
    mapping(selected["controller"]).update(
        entrypoint="scripts/main.py",
        members=[
            {"path": name, "size_bytes": len(data), "sha256": checksum(data), "role": "fixture"}
            for name, data in sorted(sources.items())
        ],
        manifest_sha256=checksum(c0_manifest),
    )
    parsed = manifest.parse_runtime_manifest(manifest_bytes)
    toolchain = mapping(selected["toolchain"])
    by_destination = {file.destination: file for file in parsed.files}
    for tool in parsed.tools:
        toolchain[tool.name] = {
            "version": tool.version,
            "executable_sha256": by_destination[tool.destination].expectation.sha256,
        }
    toolchain["runtime_manifest_sha256"] = checksum(manifest_bytes)
    toolchain["uv_lock_sha256"] = by_destination[parsed.lock].expectation.sha256
    selected_bytes = encoded(selected)
    anchor = bootstrap.ExternalSelectionAnchor(
        checksum(selected_bytes), "synthetic-offline-pin", NOW - timedelta(hours=1), NOW + timedelta(hours=1), 7
    )
    capture = bootstrap.authenticate_initial_export_selection(
        selected_bytes, c0_manifest, anchor=anchor, deployment=deployment, controller_root=controller, now=NOW
    )
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as source:
        for name in sorted(worker._REQUIRED_INPUT):
            source.writestr(name, b"{}\n")
    archive_bytes = archive.read_bytes()
    producer = mapping(selected["producer"])
    identities: list[tuple[str, str | int]] = []
    for key, value in sorted(producer.items()):
        assert type(value) in (str, int)
        assert isinstance(value, (str, int))
        identities.append((key, value))
    binding = execution.ExternalInputBinding(
        capture.selection_sha256,
        checksum(archive_bytes),
        len(archive_bytes),
        tuple(identities),
        anchor.approval_reference,
        anchor.generation,
        "synthetic-observation",
        NOW - timedelta(minutes=10),
        NOW + timedelta(minutes=10),
    )
    policy = execution.ExecutionPolicy(
        "synthetic-policy",
        7,
        10.0,
        128 * 1024**2,
        256 * 1024**2,
        256,
        1280 * 1024**2,
        512 * 1024**2,
        512 * 1024**2,
        128 * 1024**2,
        256,
        4 * 1024**3,
        24,
    )
    state = execution.ExecutionAuthorityState(anchor, deployment, policy, binding, NOW)
    return Fixture(capture, manifest_bytes, runtime, archive, SyntheticAuthority(state))


def held(candidate: Fixture) -> object:
    return execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    )


def test_full_capture_and_source_mutation_and_closed_descriptors(candidate: Fixture) -> None:
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    descriptors: list[int] = []
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as result:
        assert result.qualification == "preparatory-unattested"
        assert len(result.files) == 15
        assert result.host_tool_qualification.startswith("pending")
        assert candidate.authority.calls == 3
        assert all(file.stage == "initial" for file in result.initial_files)
        assert not any(file.role in ("tool", "raw_archive", "lock") for file in result.initial_files)
        file = next(file for file in result.files if file.destination == "/runtime/bin/python")
        original = os.pread(file.snapshot.descriptor, file.snapshot.expectation.size_bytes, 0)
        (candidate.runtime / "bin/python").write_bytes(b"mutated")
        candidate.archive.write_bytes(b"mutated archive")
        assert os.pread(file.snapshot.descriptor, len(original), 0) == original
        execution.revalidate_execution_authority(result, authority=candidate.authority)
        with result.descriptor_capacity.duplicate(file.snapshot.descriptor) as duplicate:
            assert os.pread(duplicate, len(original), 0) == original
        with result.descriptor_capacity.allocate(2, os.pipe) as pipe:
            assert len(pipe) == 2
        descriptors = [file.snapshot.descriptor for file in result.files]
    for descriptor in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("archive_sha256", "f" * 64),
        ("selection_sha256", "e" * 64),
        ("generation", 8),
        ("approval_reference", "other"),
        ("valid_before", NOW),
        ("producer", ()),
    ],
)
def test_external_binding_rejects_before_worker(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("worker must remain untouched")

    monkeypatch.setattr(worker, "capture_in_worker", forbidden)
    original = candidate.authority.state
    binding = original.input_binding
    if field in ("archive_sha256", "selection_sha256", "approval_reference"):
        assert isinstance(value, str)
        if field == "archive_sha256":
            binding = replace(binding, archive_sha256=value)
        elif field == "selection_sha256":
            binding = replace(binding, selection_sha256=value)
        else:
            binding = replace(binding, approval_reference=value)
    elif field == "generation":
        assert type(value) is int
        binding = replace(binding, generation=value)
    elif field == "valid_before":
        assert isinstance(value, datetime)
        binding = replace(binding, valid_before=value)
    else:
        binding = replace(binding, producer=())
    candidate.authority.state = replace(original, input_binding=binding)
    if field == "archive_sha256":
        # Raw archive mismatch is discovered only after sealing, not at metadata authentication.
        with (
            pytest.raises(AssertionError, match="untouched"),
            execution.authenticate_and_hold_execution_input(
                candidate.selection,
                candidate.raw_manifest,
                authority=candidate.authority,
                runtime_source=candidate.runtime,
                input_archive=candidate.archive,
            ),
        ):
            pytest.fail("unexpected capture")
        return
    with (
        pytest.raises(ValueError, match=r"identity|interval|producer"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            candidate.raw_manifest,
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")


def test_raw_runtime_digest_before_json(candidate: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_: bytes) -> manifest.RuntimeManifest:
        raise AssertionError("parser must not run")

    monkeypatch.setattr(execution, "parse_runtime_manifest", forbidden)
    with (
        pytest.raises(ValueError, match="raw runtime manifest digest"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            b"not JSON",
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")


@pytest.mark.parametrize("mutation", ["source", "extra", "symlink", "archive"])
def test_worker_rejects_mismatch_without_fd_leak(candidate: Fixture, mutation: str) -> None:
    if mutation == "source":
        (candidate.runtime / "bin/python").write_bytes(b"bad source")
    elif mutation == "extra":
        (candidate.runtime / "extra").write_bytes(b"extra")
    elif mutation == "symlink":
        (candidate.runtime / "bin/python").unlink()
        (candidate.runtime / "bin/python").symlink_to(candidate.runtime / "bin/git")
    else:
        candidate.archive.write_bytes(b"not the bound archive")
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    with (
        pytest.raises(ValueError, match="worker rejected"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            candidate.raw_manifest,
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize(
    "mutation", ["anchor_expiry", "revoked_generation", "schema", "policy", "input_expiry", "producer"]
)
def test_authority_refresh_rejects(candidate: Fixture, mutation: str) -> None:
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as result:
        assert result.files
        state = candidate.authority.state
        if mutation == "anchor_expiry":
            candidate.authority.state = replace(state, trusted_utc=state.anchor.valid_before)
        elif mutation == "revoked_generation":
            candidate.authority.state = replace(
                state, deployment=replace(state.deployment, allowed_generations=frozenset())
            )
        elif mutation == "schema":
            candidate.authority.state = replace(
                state, deployment=replace(state.deployment, selection_schema_bytes=b"{}")
            )
        elif mutation == "policy":
            candidate.authority.state = replace(
                state, execution_policy=replace(state.execution_policy, reference="changed")
            )
        elif mutation == "input_expiry":
            candidate.authority.state = replace(state, trusted_utc=state.input_binding.valid_before)
        else:
            candidate.authority.state = replace(state, input_binding=replace(state.input_binding, producer=()))
        with pytest.raises(ValueError, match=r"validity|generation|schema|authority|producer"):
            execution.revalidate_execution_authority(result, authority=candidate.authority)


def test_blocked_scan_has_parent_deadline_and_reaped_worker(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = candidate.authority.state
    candidate.authority.state = replace(
        original, execution_policy=replace(original.execution_policy, capture_seconds=0.15)
    )

    def blocked(*args: object, **kwargs: object) -> None:
        time.sleep(60)

    monkeypatch.setattr(worker, "_scan", blocked)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    start = time.monotonic()
    with (
        pytest.raises(ValueError, match="hard deadline"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            candidate.raw_manifest,
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")
    assert time.monotonic() - start < 3
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before
    with pytest.raises(ChildProcessError, match="No child processes"):
        os.waitpid(-1, os.WNOHANG)


def test_low_real_descriptor_limit_fail_closed(candidate: Fixture) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, soft), hard))
        with (
            pytest.raises(ValueError, match="RLIMIT_NOFILE"),
            execution.authenticate_and_hold_execution_input(
                candidate.selection,
                candidate.raw_manifest,
                authority=candidate.authority,
                runtime_source=candidate.runtime,
                input_archive=candidate.archive,
            ),
        ):
            pytest.fail("unexpected capture")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.parametrize("name", ["../bad", "a/../bad", "/absolute", "a\\bad"])
def test_zip_path_rejection(name: str) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, b"content")
    budget = SnapshotBudget(1024, 2048, time.monotonic() + 5)
    with pytest.raises(ValueError, match="path"):
        worker.inspect_zip(stream, maximum_members=256, maximum_expanded=2048, maximum_file=1024, budget=budget)


def test_zip_expansion_limit() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large", b"x" * 2048)
    with pytest.raises(ValueError, match="integer"):
        worker.inspect_zip(
            stream,
            maximum_members=256,
            maximum_expanded=4096,
            maximum_file=1024,
            budget=SnapshotBudget(1024, 4096, time.monotonic() + 5),
        )


@pytest.mark.parametrize("location", ["scan", "zip", "elf"])
def test_every_worker_data_phase_has_hard_deadline(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    state = candidate.authority.state
    candidate.authority.state = replace(state, execution_policy=replace(state.execution_policy, capture_seconds=0.15))

    def blocked(*args: object, **kwargs: object) -> None:
        time.sleep(60)

    monkeypatch.setattr(worker, {"scan": "_scan", "zip": "inspect_zip", "elf": "validate_elf_graph"}[location], blocked)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    with (
        pytest.raises(ValueError, match="hard deadline"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            candidate.raw_manifest,
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize(
    "message", [b'{"kind":"done"}', b'{"kind":"file"}', b"not-json", b'{"kind":"error","extra":1}']
)
def test_worker_bad_protocol_never_returns_capture(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch, message: bytes
) -> None:
    def malformed(channel: object, *args: object, **kwargs: object) -> None:
        assert isinstance(channel, socket.socket)
        channel.send(message)
        os._exit(0)

    monkeypatch.setattr(worker, "_worker", malformed)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    with (
        pytest.raises(ValueError, match=r"closure|manifest|JSON"),
        execution.authenticate_and_hold_execution_input(
            candidate.selection,
            candidate.raw_manifest,
            authority=candidate.authority,
            runtime_source=candidate.runtime,
            input_archive=candidate.archive,
        ),
    ):
        pytest.fail("unexpected capture")
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


def test_descriptor_capacity_cleanup_and_failed_factory() -> None:
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    capacity = execution.DescriptorCapacity(4)
    try:

        def rejected() -> tuple[int, ...]:
            raise OSError("synthetic failed allocation")

        with pytest.raises(OSError, match="synthetic failed allocation"), capacity.allocate(2, rejected):
            pytest.fail("unexpected lease")
        with capacity.infrastructure(2):
            source, sink = os.pipe()
            try:
                assert os.write(sink, b"ok") == 2
                assert os.read(source, 2) == b"ok"
            finally:
                os.close(source)
                os.close(sink)
        with capacity.allocate(2, os.pipe) as descriptors:
            assert len(descriptors) == 2
        with pytest.raises(ValueError, match="exhausted"), capacity.infrastructure(5):
            pytest.fail("unexpected capacity")
    finally:
        capacity.close()
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize("case", ["duplicate", "collision", "special", "empty_directory", "bad_crc", "too_many"])
def test_zip_structural_negative_cases(case: str) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("file", b"data")
        if case == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("file", b"data")
        elif case == "collision":
            archive.writestr("file/child", b"data")
        elif case == "special":
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"target")
        elif case == "empty_directory":
            archive.writestr("unused/", b"")
        elif case == "too_many":
            archive.writestr("extra", b"data")
    if case == "bad_crc":
        data = stream.getvalue().replace(b"data", b"evil", 1)
        stream = io.BytesIO(data)
    with pytest.raises((ValueError, zipfile.BadZipFile), match=r"ZIP|graph|CRC"), pytest.MonkeyPatch.context():
        worker.inspect_zip(
            stream,
            maximum_members=1 if case == "too_many" else 256,
            maximum_expanded=4096,
            maximum_file=1024,
            budget=SnapshotBudget(1024, 4096, time.monotonic() + 5),
        )


@pytest.mark.parametrize(
    "case", ["undeclared", "wrong_magic", "bad_flags", "cache", "native", "split_package", "cp437_missing"]
)
def test_runtime_zip_code_layout_rejects(tmp_path: Path, case: str) -> None:

    raw, _ = runtime_fixture()
    parsed = manifest.parse_runtime_manifest(encoded(raw))
    stream = io.BytesIO()
    bytecode = bytes.fromhex("a70d0d0a") + bytes(12) + b"not executed"
    entries = {"encodings/__init__.py": b"# fixture", "encodings/cp437.py": b"# fixture"}
    if case in ("undeclared", "wrong_magic", "bad_flags"):
        if case == "wrong_magic":
            bytecode = b"bad!" + bytecode[4:]
        if case == "bad_flags":
            bytecode = bytecode[:4] + (8).to_bytes(4, "little") + bytecode[8:]
        entries["selected.pyc"] = bytecode
        if case != "undeclared":
            expected = FileExpectation(len(bytecode), checksum(bytecode), 0o400)
            parsed = replace(parsed, sourceless_modules=(("selected.pyc", expected),))
    elif case == "cache":
        entries["__pycache__/cached.cpython-311.pyc"] = bytecode
    elif case == "native":
        entries["native.so"] = b"ELF sentinel"
    elif case == "split_package":
        entries["rpds/__init__.py"] = b'raise RuntimeError("must not execute")'
        parsed = replace(parsed, packages=(manifest.RuntimePackage("rpds", "/runtime/site/rpds", "regular"),))
    else:
        del entries["encodings/cp437.py"]
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    data = stream.getvalue()
    with capture_stream(
        io.BytesIO(data),
        FileExpectation(len(data), checksum(data), 0o400),
        budget=SnapshotBudget(65536, 65536, time.monotonic() + 5),
    ) as snapshot:
        file = execution.MountedSnapshot(parsed.python_zip, snapshot, "initial", "python_zip")
        with pytest.raises(ValueError, match=r"runtime ZIP|runtime sourceless"):
            worker._validate_runtime_zip((file,), parsed, SnapshotBudget(65536, 65536, time.monotonic() + 5))


@pytest.mark.parametrize(
    "mutation",
    ["replace_input", "drop_input", "extra_input", "stage", "parents", "elf", "native_metadata", "data_metadata"],
)
def test_gate_reconstructs_authenticated_retained_file_closure(candidate: Fixture, mutation: str) -> None:
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as original:
        assert original.files
        input_file = next(file for file in original.files if file.role == "input")
        data = b"forged validly sealed input"
        with capture_stream(
            io.BytesIO(data),
            FileExpectation(len(data), checksum(data), 0o400),
            budget=SnapshotBudget(65536, 65536, time.monotonic() + 5),
        ) as forged_snapshot:
            forged_file = replace(input_file, snapshot=forged_snapshot)
            if mutation == "replace_input":
                altered = replace(
                    original, files=tuple(forged_file if file == input_file else file for file in original.files)
                )
            elif mutation == "drop_input":
                altered = replace(original, files=tuple(file for file in original.files if file != input_file))
            elif mutation == "extra_input":
                altered = replace(
                    original, files=(*original.files, replace(forged_file, destination="/input/evidence/forged.json"))
                )
            elif mutation == "stage":
                altered = replace(
                    original,
                    files=tuple(replace(file, stage="host") if file == input_file else file for file in original.files),
                )
            elif mutation == "parents":
                altered = replace(original, parent_directories=("/proc/forged",))
            elif mutation == "native_metadata":
                altered = replace(
                    original,
                    files=tuple(replace(file, elf_metadata=None) for file in original.files),
                )
            elif mutation == "data_metadata":
                altered = replace(
                    original,
                    files=tuple(
                        replace(file, elf_metadata=ElfMetadata(None, (), None, ())) if file == input_file else file
                        for file in original.files
                    ),
                )
            else:
                altered = replace(original, elf=())
            with pytest.raises(ValueError, match="validation worker rejected"):
                execution.revalidate_execution_authority(altered, authority=candidate.authority)


def test_gate_validation_worker_stall_has_hard_deadline(candidate: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as original:
        assert original.files
        shortened = replace(original, capture_deadline=time.monotonic() + 0.15)

        def blocked(*args: object, **kwargs: object) -> None:
            time.sleep(60)

        monkeypatch.setattr(worker, "inspect_zip", blocked)
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        with pytest.raises(ValueError, match="hard deadline"):
            execution.revalidate_execution_authority(shortened, authority=candidate.authority)
        assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize("change", ["expiry", "revoke", "binding"])
def test_authority_refresh_after_parser_keeps_gate_closed(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as result:
        assert result.files
        original_validation = worker.validate_held_in_worker

        def changed_after_parse(value: execution.HeldControllerExecution) -> None:
            original_validation(value)
            state = candidate.authority.state
            if change == "expiry":
                candidate.authority.state = replace(state, trusted_utc=state.anchor.valid_before)
            elif change == "revoke":
                candidate.authority.state = replace(
                    state, deployment=replace(state.deployment, allowed_generations=frozenset())
                )
            else:
                candidate.authority.state = replace(
                    state, input_binding=replace(state.input_binding, observation_reference="changed")
                )

        monkeypatch.setattr(worker, "validate_held_in_worker", changed_after_parse)
        with pytest.raises(ValueError, match=r"validity|generation|authority changed"):
            execution.revalidate_execution_authority(result, authority=candidate.authority)


@pytest.mark.parametrize("case", ["valid", "namespace_initializer", "namespace_module", "native_module"])
def test_declared_bytecode_logical_package_identities(case: str) -> None:
    import marshal

    raw, _ = runtime_fixture()
    parsed = manifest.parse_runtime_manifest(encoded(raw))
    payload = marshal.dumps(
        compile('raise RuntimeError("selected sentinel must not execute")', "selected-fixture", "exec")
    )
    bytecode = bytes.fromhex("a70d0d0a") + bytes(12) + payload
    name = "selected.pyc"
    if case == "namespace_initializer":
        name = "namespace_fixture/__init__.pyc"
    elif case == "namespace_module":
        name = "namespace_fixture.pyc"
    elif case == "native_module":
        name = "_native_fixture.pyc"
    expected = FileExpectation(len(bytecode), checksum(bytecode), 0o400)
    parsed = replace(parsed, sourceless_modules=((name, expected),))
    if case.startswith("namespace"):
        parsed = replace(
            parsed,
            packages=(
                manifest.RuntimePackage("namespace_fixture", parsed.package_root + "/namespace_fixture", "namespace"),
            ),
        )
    data_stream = io.BytesIO()
    with zipfile.ZipFile(data_stream, "w") as archive:
        archive.writestr("encodings/__init__.py", b"# fixture")
        archive.writestr("encodings/cp437.py", b"# fixture")
        archive.writestr(name, bytecode)
    data = data_stream.getvalue()
    with capture_stream(
        io.BytesIO(data),
        FileExpectation(len(data), checksum(data), 0o400),
        budget=SnapshotBudget(65536, 65536, time.monotonic() + 5),
    ) as snapshot:
        files: tuple[execution.MountedSnapshot, ...] = (
            execution.MountedSnapshot(parsed.python_zip, snapshot, "initial", "python_zip"),
        )
        if case == "native_module":
            files = (
                *files,
                execution.MountedSnapshot(
                    parsed.extension_root + "/_native_fixture.cpython-311-x86_64-linux-gnu.so",
                    snapshot,
                    "initial",
                    "extension",
                ),
            )
        budget = SnapshotBudget(65536, 65536, time.monotonic() + 5)
        if case == "valid":
            worker._validate_runtime_zip(files, parsed, budget)
            assert snapshot.expectation.sha256 == checksum(data)
        else:
            with pytest.raises(ValueError, match=r"shadows|splits"):
                worker._validate_runtime_zip(files, parsed, budget)


@pytest.mark.parametrize("case", ["native_missing", "data_has_metadata", "extra_fields", "bad_needed"])
def test_worker_elf_observation_closed_protocol(case: str) -> None:
    row: dict[str, object] = {"interpreter": None, "needed": [], "soname": None, "search_paths": []}
    value: object = row
    if case == "native_missing":
        value = None
    elif case == "extra_fields":
        row["extra"] = True
    elif case == "bad_needed":
        row["needed"] = [True]
    with pytest.raises(ValueError, match=r"manifest|native"):
        worker._elf_observation(value, native=case != "data_has_metadata")


def test_initial_native_parser_is_only_in_supervised_worker(
    candidate: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Capture worker uses its own imported canonical parser. Gate worker must
    # use the real parser in its child; prohibit only parent PID, not children.
    parent_pid = os.getpid()
    original = validate_elf_graph

    def only_child(
        runtime_manifest: manifest.RuntimeManifest, descriptors: dict[str, int]
    ) -> tuple[tuple[str, ElfMetadata], ...]:
        if os.getpid() == parent_pid:
            raise AssertionError("parent native parsing must remain untouched")
        return original(runtime_manifest, descriptors)

    monkeypatch.setattr(execution, "validate_elf_graph", only_child)
    with execution.authenticate_and_hold_execution_input(
        candidate.selection,
        candidate.raw_manifest,
        authority=candidate.authority,
        runtime_source=candidate.runtime,
        input_archive=candidate.archive,
    ) as result:
        assert len(result.elf) == 4
        assert all(file.elf_metadata is not None for file in result.files if file.role in manifest.NATIVE_ROLES)
        assert all(file.elf_metadata is None for file in result.files if file.role not in manifest.NATIVE_ROLES)
