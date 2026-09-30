"""Authenticate and retain execution bytes; independent deployment remains mandatory.

All quotas are preparatory ceilings. A matching fixture is neither deployment
approval nor a qualified toolchain, backend, workflow or release.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_controller_bootstrap import (
    BootstrapDeploymentPolicy,
    ExternalSelectionAnchor,
    SelectionBoundControllerCapture,
    validate_retained_selection,
)
from scripts.release_controller_elf import ElfMetadata, validate_elf_graph
from scripts.release_controller_runtime_manifest import (
    MAX_MANIFEST_BYTES,
    MAX_RUNTIME_BYTES,
    MAX_RUNTIME_FILE_BYTES,
    MAX_RUNTIME_FILES,
    RuntimeManifest,
    bounded_int,
    digest,
    object_fields,
    parse_runtime_manifest,
    text,
    validate_file_graph,
)
from scripts.release_controller_snapshot import (
    FileExpectation,
    SealedFileSnapshot,
    preflight_snapshot_descriptors,
    validate_snapshot_descriptor,
)

MAX_CAPTURE_SECONDS = 120.0
MAX_SNAPSHOT_BYTES = 1280 * 1024 * 1024
MAX_INPUT_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_INPUT_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 128 * 1024 * 1024
MAX_INPUT_FILES = 256
MAX_HELD_FILES = 769
# Serialize this boundary's descriptor allocation over the entire borrowed lifetime.
_ALLOCATOR = threading.Lock()


@dataclass(frozen=True)
class ExternalInputBinding:
    """Independently supplied raw artifact observation; construction grants no trust."""

    selection_sha256: str
    archive_sha256: str
    archive_size_bytes: int
    producer: tuple[tuple[str, str | int], ...]
    approval_reference: str
    generation: int
    observation_reference: str
    valid_after: datetime
    valid_before: datetime


@dataclass(frozen=True)
class ExecutionPolicy:
    """Explicit deployment-owned reductions of fixed, still preparatory ceilings."""

    reference: str
    generation: int
    capture_seconds: float
    runtime_file_bytes: int
    runtime_total_bytes: int
    runtime_files: int
    snapshot_total_bytes: int
    archive_bytes: int
    input_expanded_bytes: int
    input_file_bytes: int
    input_files: int
    worker_address_space_bytes: int
    descriptor_headroom: int


@dataclass(frozen=True)
class ExecutionAuthorityState:
    anchor: ExternalSelectionAnchor
    deployment: BootstrapDeploymentPolicy
    execution_policy: ExecutionPolicy
    input_binding: ExternalInputBinding
    trusted_utc: datetime


class TrustedExecutionAuthority(ABC):
    """Interface supplied by independently controlled trusted host deployment.

    Implementations must bound acquisition. Candidate callables and self-described
    policy objects do not establish independent control or approval provenance.
    """

    @abstractmethod
    def current_state(self) -> ExecutionAuthorityState:
        """Acquire current anchor, policies, artifact binding and trusted UTC."""


@dataclass(frozen=True)
class MountedSnapshot:
    destination: str
    snapshot: SealedFileSnapshot
    stage: str
    role: str
    elf_metadata: ElfMetadata | None = None


class DescriptorCapacity:
    """Owned allocation slots; serialize consumption, without changing host limits.

    This reserves actual descriptors for owned duplicate and infrastructure
    allocations. It does not reserve descriptors against unrelated host threads.
    """

    def __init__(self, count: int) -> None:
        self._reserved: list[int] = []
        self._active: set[int] = set()
        self._lock = threading.Lock()
        self._base = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        try:
            for _ in range(count):
                self._reserved.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for descriptor in (*self._reserved, *self._active):
            os.close(descriptor)
        self._reserved.clear()
        self._active.clear()
        os.close(self._base)

    @contextmanager
    def allocate(self, count: int, factory: Callable[[], tuple[int, ...]]) -> Iterator[tuple[int, ...]]:
        """Use reserved capacity for a bounded descriptor-producing operation.

        Factory must clean its partial failures and return exactly count owned
        descriptors. Runner keeps the lease until verified namespace death.
        """
        with self._lock:
            if type(count) is not int or not 0 < count <= len(self._reserved):
                raise ValueError("descriptor reservation is exhausted")
            for _ in range(count):
                os.close(self._reserved.pop())
            try:
                descriptors = factory()
                if len(descriptors) != count or len(set(descriptors)) != count:
                    for descriptor in set(descriptors):
                        os.close(descriptor)
                    raise ValueError("descriptor factory returned invalid allocation")
                self._active.update(descriptors)
            except BaseException:
                for _ in range(count):
                    self._reserved.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
                raise
        try:
            yield descriptors
        finally:
            with self._lock:
                for descriptor in descriptors:
                    os.close(descriptor)
                    self._active.remove(descriptor)
                for _ in range(count):
                    self._reserved.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))

    @contextmanager
    def infrastructure(self, count: int) -> Iterator[None]:
        """Release exact reserved capacity to independently owned infrastructure.

        The trusted runner enumerates its peak allocations before this lease.
        It owns and closes Popen/selector/pipe/memfd/pidfd objects normally,
        including partial failures, before lease exit. This capacity object
        never closes their descriptors, avoiding double-close or fd reuse.
        """
        with self._lock:
            if type(count) is not int or not 0 < count <= len(self._reserved):
                raise ValueError("descriptor infrastructure reservation is exhausted")
            for _ in range(count):
                os.close(self._reserved.pop())
        try:
            yield
        finally:
            with self._lock:
                for _ in range(count):
                    self._reserved.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))

    @contextmanager
    def duplicate(self, source: int) -> Iterator[int]:
        with self._lock:
            if not self._reserved:
                raise ValueError("descriptor reservation is exhausted")
            slot = self._reserved.pop()
            try:
                os.dup2(source, slot, inheritable=False)
            except BaseException:
                self._reserved.append(slot)
                raise
            self._active.add(slot)
        try:
            yield slot
        finally:
            with self._lock:
                os.dup2(self._base, slot, inheritable=False)
                self._active.remove(slot)
                self._reserved.append(slot)


@dataclass(frozen=True)
class HeldControllerExecution:
    """Borrowed sealed files valid only while the owning context remains open."""

    selection: SelectionBoundControllerCapture
    runtime_manifest_bytes: bytes
    runtime_manifest: RuntimeManifest
    authority_state: ExecutionAuthorityState
    files: tuple[MountedSnapshot, ...]
    parent_directories: tuple[str, ...]
    elf: tuple[tuple[str, ElfMetadata], ...]
    capture_deadline: float
    descriptor_capacity: DescriptorCapacity
    qualification: str = "preparatory-unattested"
    host_tool_qualification: str = "pending-independent-host-execution-qualification"

    @property
    def initial_files(self) -> tuple[MountedSnapshot, ...]:
        return tuple(file for file in self.files if file.stage == "initial")


def _validate_policy(policy: ExecutionPolicy) -> None:
    if not isinstance(policy, ExecutionPolicy):
        raise ValueError("explicit execution deployment policy is required")
    text(policy.reference)
    bounded_int(policy.generation, 2**31 - 1, minimum=1)
    if (
        type(policy.capture_seconds) not in (int, float)
        or not math.isfinite(policy.capture_seconds)
        or not 0 < policy.capture_seconds <= MAX_CAPTURE_SECONDS
    ):
        raise ValueError("execution capture deadline exceeds hard ceiling")
    for value, ceiling in (
        (policy.runtime_file_bytes, MAX_RUNTIME_FILE_BYTES),
        (policy.runtime_total_bytes, MAX_RUNTIME_BYTES),
        (policy.runtime_files, MAX_RUNTIME_FILES),
        (policy.snapshot_total_bytes, MAX_SNAPSHOT_BYTES),
        (policy.archive_bytes, MAX_INPUT_ARCHIVE_BYTES),
        (policy.input_expanded_bytes, MAX_INPUT_EXPANDED_BYTES),
        (policy.input_file_bytes, MAX_INPUT_FILE_BYTES),
        (policy.input_files, MAX_INPUT_FILES),
        (policy.worker_address_space_bytes, 4 * 1024**3),
        (policy.descriptor_headroom, 256),
    ):
        bounded_int(value, ceiling, minimum=1)
    if policy.descriptor_headroom < 16:
        raise ValueError("execution descriptor infrastructure headroom is insufficient")


def _validate_binding(
    binding: ExternalInputBinding, state: ExecutionAuthorityState, selected: Mapping[str, object], selection_sha256: str
) -> None:
    if not isinstance(binding, ExternalInputBinding):
        raise ValueError("independent raw artifact binding is required")
    digest(binding.archive_sha256)
    if (
        binding.selection_sha256 != selection_sha256
        or binding.approval_reference != state.anchor.approval_reference
        or type(binding.generation) is not int
        or binding.generation != state.anchor.generation
    ):
        raise ValueError("input binding selection/approval identity mismatch")
    text(binding.observation_reference)
    bounded_int(binding.archive_size_bytes, state.execution_policy.archive_bytes, minimum=1)
    times = (state.trusted_utc, binding.valid_after, binding.valid_before)
    if any(not isinstance(value, datetime) or value.utcoffset() != UTC.utcoffset(None) for value in times):
        raise ValueError("input binding requires trusted UTC times")
    if not binding.valid_after <= state.trusted_utc < binding.valid_before:
        raise ValueError("input binding observation is outside its validity interval")
    producer = selected["producer"]
    if not isinstance(producer, dict) or type(binding.producer) is not tuple:
        raise ValueError("input producer identity is invalid")
    keys: list[str] = []
    observed: dict[str, str | int] = {}
    for row in binding.producer:
        if type(row) is not tuple or len(row) != 2 or type(row[0]) is not str or type(row[1]) not in (str, int):
            raise ValueError("input producer identity is invalid")
        keys.append(row[0])
        observed[row[0]] = row[1]
    if (
        keys != sorted(set(keys))
        or observed != producer
        or any(type(observed[key]) is not type(producer[key]) for key in observed)
    ):
        raise ValueError("input producer/artifact identity mismatch")


def _authenticate_state(
    selection: SelectionBoundControllerCapture, raw_manifest: bytes, state: ExecutionAuthorityState
) -> RuntimeManifest:
    if not isinstance(state, ExecutionAuthorityState):
        raise ValueError("trusted host authority state is required")
    _validate_policy(state.execution_policy)
    selected = validate_retained_selection(
        selection, anchor=state.anchor, deployment=state.deployment, now=state.trusted_utc
    )
    toolchain = selected["toolchain"]
    if not isinstance(toolchain, dict):
        raise ValueError("selected toolchain is invalid")
    if type(raw_manifest) is not bytes or not 0 < len(raw_manifest) <= MAX_MANIFEST_BYTES:
        raise ValueError("raw runtime manifest exceeds bound")
    if not hmac.compare_digest(hashlib.sha256(raw_manifest).hexdigest(), digest(toolchain["runtime_manifest_sha256"])):
        raise ValueError("raw runtime manifest digest disagrees with selection")
    manifest = parse_runtime_manifest(raw_manifest)
    policy = state.execution_policy
    if (
        len(manifest.files) > policy.runtime_files
        or sum(file.expectation.size_bytes for file in manifest.files) > policy.runtime_total_bytes
        or any(file.expectation.size_bytes > policy.runtime_file_bytes for file in manifest.files)
    ):
        raise ValueError("runtime closure exceeds deployed policy")
    by_destination = {file.destination: file for file in manifest.files}
    for tool in manifest.tools:
        selected_tool = object_fields(toolchain[tool.name], {"version", "executable_sha256"})
        if (
            tool.version != selected_tool["version"]
            or by_destination[tool.destination].expectation.sha256 != selected_tool["executable_sha256"]
        ):
            raise ValueError("runtime tool identity disagrees with selection")
    if by_destination[manifest.lock].expectation.sha256 != toolchain["uv_lock_sha256"]:
        raise ValueError("runtime lock digest disagrees with selection")
    _validate_binding(state.input_binding, state, selected, selection.selection_sha256)
    return manifest


def validate_held_execution_closure(execution: HeldControllerExecution) -> None:
    """Pure retained-descriptor inspection, supervised by the host worker caller."""
    from scripts.release_controller_capture_worker import inspect_zip
    from scripts.release_controller_snapshot import SnapshotBudget

    if type(execution.files) is not tuple or not 0 < len(execution.files) <= MAX_HELD_FILES:
        raise ValueError("held execution file count is invalid")
    for file in execution.files:
        if not isinstance(file, MountedSnapshot) or not isinstance(file.snapshot, SealedFileSnapshot):
            raise ValueError("held execution file is invalid")
        validate_snapshot_descriptor(
            file.snapshot.descriptor,
            file.snapshot.expectation,
            deadline=execution.capture_deadline,
            identity=file.snapshot.identity,
        )
    actual = {file.destination: (file.snapshot.expectation, file.stage, file.role) for file in execution.files}
    if len(actual) != len(execution.files):
        raise ValueError("held execution contains duplicate destinations")
    manifest = execution.runtime_manifest
    expected: dict[str, tuple[FileExpectation, str, str]] = {
        file.destination: (file.expectation, file.stage, file.role) for file in manifest.files
    }
    expected.update(
        {
            "/controller/" + path: (
                FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400),
                "initial",
                "controller",
            )
            for path, data in execution.selection.controller.members
        }
    )
    binding = execution.authority_state.input_binding
    raw_path = "/retained/raw-input.zip"
    raw_expected = FileExpectation(binding.archive_size_bytes, binding.archive_sha256, 0o400)
    expected[raw_path] = (raw_expected, "host", "raw_archive")
    if actual.get(raw_path) != expected[raw_path]:
        raise ValueError("held raw archive disagrees with independent binding")
    raw = next(file.snapshot for file in execution.files if file.destination == raw_path)
    policy = execution.authority_state.execution_policy
    budget = SnapshotBudget(
        maximum_file_bytes=policy.input_file_bytes,
        maximum_total_bytes=policy.input_expanded_bytes,
        deadline=execution.capture_deadline,
    )
    with execution.descriptor_capacity.allocate(
        1, lambda: (os.open(f"/proc/self/fd/{raw.descriptor}", os.O_RDONLY | os.O_CLOEXEC),)
    ) as readers:
        validate_snapshot_descriptor(
            readers[0], raw_expected, deadline=execution.capture_deadline, identity=raw.identity
        )
        with os.fdopen(readers[0], "rb", closefd=False) as stream:
            entries = inspect_zip(
                stream,
                maximum_members=policy.input_files,
                maximum_expanded=policy.input_expanded_bytes,
                maximum_file=policy.input_file_bytes,
                budget=budget,
            )
    expected.update({"/input/" + path: (expectation, "initial", "input") for path, expectation in entries})
    if actual != expected:
        raise ValueError("held execution file closure disagrees with authenticated bytes")
    if (
        sum(file.snapshot.expectation.size_bytes for file in execution.files if file.role != "controller")
        > policy.snapshot_total_bytes
    ):
        raise ValueError("held execution snapshot budget exceeded")
    if execution.parent_directories != validate_file_graph(tuple(file.destination for file in execution.initial_files)):
        raise ValueError("held execution parent graph disagrees with exact file closure")
    descriptors = {file.destination: file.snapshot.descriptor for file in execution.files if file.role != "raw_archive"}
    native_graph = validate_elf_graph(manifest, descriptors)
    observations = tuple(
        sorted((file.destination, file.elf_metadata) for file in execution.files if file.elf_metadata is not None)
    )
    if execution.elf != native_graph or observations != native_graph:
        raise ValueError("held execution native graph disagrees with exact file closure")
    budget.check_deadline()


def revalidate_execution_authority(execution: HeldControllerExecution, *, authority: TrustedExecutionAuthority) -> None:
    """Refresh at the closed startup gate; revalidate retained bytes, never sources."""
    if not isinstance(authority, TrustedExecutionAuthority):
        raise ValueError("trusted execution authority provider is required")
    state = authority.current_state()
    manifest = _authenticate_state(execution.selection, execution.runtime_manifest_bytes, state)
    original = execution.authority_state
    if (
        state.anchor != original.anchor
        or state.deployment != original.deployment
        or state.execution_policy != original.execution_policy
        or state.input_binding != original.input_binding
        or manifest != execution.runtime_manifest
    ):
        raise ValueError("execution authority changed after capture")
    if time.monotonic() >= execution.capture_deadline:
        raise ValueError("execution capture/startup deadline exceeded")
    from scripts.release_controller_capture_worker import validate_held_in_worker

    validate_held_in_worker(execution)
    final_state = authority.current_state()
    final_manifest = _authenticate_state(execution.selection, execution.runtime_manifest_bytes, final_state)
    if (
        final_state.anchor != original.anchor
        or final_state.deployment != original.deployment
        or final_state.execution_policy != original.execution_policy
        or final_state.input_binding != original.input_binding
        or final_manifest != manifest
    ):
        raise ValueError("execution authority changed during startup validation")
    if time.monotonic() >= execution.capture_deadline:
        raise ValueError("execution capture/startup deadline exceeded")


@contextmanager
def authenticate_and_hold_execution_input(
    selection: SelectionBoundControllerCapture,
    runtime_manifest_bytes: bytes,
    *,
    authority: TrustedExecutionAuthority,
    runtime_source: Path,
    input_archive: Path,
) -> Iterator[HeldControllerExecution]:
    """Capture only through supervised host tooling, without selected execution."""
    if not isinstance(authority, TrustedExecutionAuthority):
        raise ValueError("trusted execution authority provider is required")
    state = authority.current_state()
    manifest = _authenticate_state(selection, runtime_manifest_bytes, state)
    deadline = time.monotonic() + state.execution_policy.capture_seconds
    # Import only independently deployed host code; no selected import roots exist.
    from scripts.release_controller_capture_worker import capture_in_worker

    with _ALLOCATOR, ExitStack() as stack:
        maximum_files = len(manifest.files) + len(selection.controller.members) + state.execution_policy.input_files + 1
        headroom = state.execution_policy.descriptor_headroom
        preflight_snapshot_descriptors(
            source_descriptors=34,
            retained_readers=maximum_files,
            sealing_writers=1,
            overlapping_readers=1,
            runner_duplicates=maximum_files,
            infrastructure_descriptors=headroom + 1,
        )
        # Actual held descriptors reserve runner duplication/headroom capacity;
        # runner consumes these reservations under explicit transferred ownership.
        capacity = DescriptorCapacity(maximum_files + headroom)
        stack.callback(capacity.close)
        files = stack.enter_context(
            capture_in_worker(selection, manifest, state, runtime_source, input_archive, deadline=deadline)
        )
        parents = validate_file_graph(tuple(file.destination for file in files if file.stage == "initial"))
        elf = tuple(sorted((file.destination, file.elf_metadata) for file in files if file.elf_metadata is not None))
        held = HeldControllerExecution(
            selection, runtime_manifest_bytes, manifest, state, files, parents, elf, deadline, capacity
        )
        revalidate_execution_authority(held, authority=authority)
        yield held
