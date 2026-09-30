"""Pure phase-bound rehearsal decoding; no execution or proof approval.

Only the initial public cutover identity is registered. Successor subjects are
supported for private structural diagnostics, not a future public observer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Literal, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import Schema

from scripts import documentation_commands, documentation_manual
from scripts.artifact_limits import MAX_ARTIFACT_BYTES
from scripts.json_policy import load_json_bytes

# Match the existing retained release-evidence acquisition limits. These are
# parsing ceilings, not approval of any runtime's resource enforcement.
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_RETAINED_MEMBERS = 4096
Phase = Literal["private-candidate", "public-release"]
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, kw_only=True)
class InitialExportSubject:
    private_source_sha: str
    private_source_tree: str
    exported_tree: str
    documentation_contract_sha256: str
    source_kind: Literal["initial-export"] = "initial-export"


@dataclass(frozen=True, kw_only=True)
class PublicHistorySubject:
    """Current source and historical anchor identity, not ancestry proof.

    The caller supplies expectations from independently verified canonical
    PublicHistorySource bytes and its approved cutover anchor. Typed values and
    digest equality alone do not authenticate source, history, or the record.
    """

    repository: str
    repository_id: int
    source_commit: str
    source_tree: str
    version: str
    planned_tag: str
    source_sha256: str
    initial_commit: str
    initial_tree: str
    record_sha256: str
    documentation_contract_sha256: str
    source_kind: Literal["public-history"] = "public-history"


SubjectBinding = InitialExportSubject | PublicHistorySubject


@dataclass(frozen=True, kw_only=True)
class ControlBinding:
    controller_revision: str
    controller_set_sha256: str
    registry_sha256: str
    schema_sha256: str
    toolchain_sha256: str
    policy_sha256: str
    selection_record_sha256: str


@dataclass(frozen=True, kw_only=True)
class ArtifactIdentity:
    name: str
    kind: Literal["wheel", "sdist"]
    size: int
    sha256: str


@dataclass(frozen=True, kw_only=True)
class MemberIdentity:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True, kw_only=True)
class PublicBinding:
    repository: str
    repository_id: int
    commit_sha: str
    tree: str
    run_id: int
    run_attempt: int
    workflow_id: int


@dataclass(frozen=True, kw_only=True)
class ResourceLimits:
    processes: int
    memory_bytes: int
    cpu_seconds: int
    scratch_bytes: int
    output_bytes: int
    wall_seconds: int


@dataclass(frozen=True, kw_only=True)
class ProtectedRehearsalContext:
    """Expectations selected by the caller before loading control inputs.

    Constructing this object or matching its hashes does not authenticate the
    caller, approve a backend, or register a behavioral verifier.
    """

    phase: Phase
    subject: SubjectBinding
    control: ControlBinding
    artifacts: tuple[ArtifactIdentity, ...]
    resources: ResourceLimits
    public: PublicBinding | None
    private_receipt_sha256: str | None


@dataclass(frozen=True, kw_only=True)
class StreamObservation:
    full_sha256: str
    total_bytes: int
    capture_limit_bytes: int
    truncated: bool
    redacted: bool
    retained: MemberIdentity


@dataclass(frozen=True, kw_only=True)
class ExecutionObservation:
    argv: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    resources: ResourceLimits
    exit_code: int | None
    signal: int | None
    timed_out: bool
    deadline_seconds: int
    policy_verdict: Literal["allow", "deny", "unavailable"]
    policy_complete: bool
    lost_events: int
    policy_observation: MemberIdentity | None
    teardown_result: Literal["verified-empty", "failed", "unavailable"]
    teardown_observation: MemberIdentity | None
    stdout: StreamObservation
    stderr: StreamObservation
    assertions: tuple[documentation_commands.TranscriptAssertion, ...]


@dataclass(frozen=True, kw_only=True)
class ScenarioObservation:
    identifier: str
    precondition_id: str
    block_sha256: str | None
    actor: str
    commands: tuple[ExecutionObservation, ...]


@dataclass(frozen=True, kw_only=True)
class RehearsalValidation:
    phase: Phase
    subject: SubjectBinding
    control: ControlBinding
    artifacts: tuple[ArtifactIdentity, ...]
    scenarios: tuple[ScenarioObservation, ...]
    public: PublicBinding | None
    pending_scenarios: tuple[str, ...]
    missing_scenarios: tuple[str, ...]
    issues: tuple[str, ...]
    schema_evaluation_performed: bool
    linked_private: RehearsalValidation | None = None
    verified_blocks: tuple[str, ...] = ()

    @property
    def passing(self) -> Literal[False]:
        """No approved behavioral verifier/backend exists in this slice."""
        return False


def _json(data: bytes) -> dict[str, JsonValue]:
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError("rehearsal input exceeds byte limit")
    # The shared JSON decoder only constructs JSON primitives; check the root
    # before narrowing its recursive type at this library boundary.
    return cast(dict[str, JsonValue], _object(load_json_bytes(data)))


def _object(value: object, keys: tuple[str, ...] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("rehearsal object is invalid")
    if keys is not None and set(value) != set(keys):
        raise ValueError("rehearsal object fields are invalid")
    return value


def _list(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("rehearsal list is invalid")
    return value


def _string(value: object, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("rehearsal string is invalid")
    return value


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError("rehearsal integer is invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("rehearsal boolean is invalid")
    return value


def _digest(value: object, length: int = 64) -> str:
    result = _string(value)
    if len(result) != length or any(character not in "0123456789abcdef" for character in result):
        raise ValueError("rehearsal digest is invalid")
    return result


def _member(value: object) -> MemberIdentity:
    raw = _object(value, ("name", "sha256", "size"))
    name = _string(raw["name"])
    parts = PurePosixPath(name).parts
    if (
        not parts
        or name[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        or name.startswith("/")
        or PurePosixPath(name).as_posix() != name
        or any(part in {".", ".."} for part in parts)
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-" for character in name
        )
    ):
        raise ValueError("rehearsal member name is invalid")
    return MemberIdentity(name=name, sha256=_digest(raw["sha256"]), size=_integer(raw["size"]))


def _optional_member(value: object) -> MemberIdentity | None:
    return None if value is None else _member(value)


def _subject(value: object) -> SubjectBinding:
    subject = _object(value)
    if subject.get("source_kind") == "public-history":
        raw = _object(
            subject,
            (
                "source_kind",
                "repository",
                "repository_id",
                "source_commit",
                "source_tree",
                "version",
                "planned_tag",
                "source_sha256",
                "initial_commit",
                "initial_tree",
                "record_sha256",
                "documentation_contract_sha256",
            ),
        )
        version = _string(raw["version"], 32)
        if (
            re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None
            or tuple(int(part) for part in version.split(".")) <= (1, 0, 0)
            or raw["planned_tag"] != f"v{version}"
            or raw["repository"] != "mpeter/fieldkit-cli"
        ):
            raise ValueError("rehearsal history release identity is invalid")
        return PublicHistorySubject(
            repository="mpeter/fieldkit-cli",
            repository_id=_integer(raw["repository_id"], 1),
            source_commit=_digest(raw["source_commit"], 40),
            source_tree=_digest(raw["source_tree"], 40),
            version=version,
            planned_tag=f"v{version}",
            source_sha256=_digest(raw["source_sha256"]),
            initial_commit=_digest(raw["initial_commit"], 40),
            initial_tree=_digest(raw["initial_tree"], 40),
            record_sha256=_digest(raw["record_sha256"]),
            documentation_contract_sha256=_digest(raw["documentation_contract_sha256"]),
        )
    if subject.get("source_kind") != "initial-export":
        raise ValueError("rehearsal source kind is invalid")
    raw = _object(
        subject,
        ("source_kind", "private_source_sha", "private_source_tree", "exported_tree", "documentation_contract_sha256"),
    )
    return InitialExportSubject(
        private_source_sha=_digest(raw["private_source_sha"], 40),
        private_source_tree=_digest(raw["private_source_tree"], 40),
        exported_tree=_digest(raw["exported_tree"], 40),
        documentation_contract_sha256=_digest(raw["documentation_contract_sha256"]),
    )


def _control(value: object) -> ControlBinding:
    raw = _object(
        value,
        (
            "controller_revision",
            "controller_set_sha256",
            "registry_sha256",
            "schema_sha256",
            "toolchain_sha256",
            "policy_sha256",
            "selection_record_sha256",
        ),
    )
    return ControlBinding(
        controller_revision=_digest(raw["controller_revision"], 40),
        controller_set_sha256=_digest(raw["controller_set_sha256"]),
        registry_sha256=_digest(raw["registry_sha256"]),
        schema_sha256=_digest(raw["schema_sha256"]),
        toolchain_sha256=_digest(raw["toolchain_sha256"]),
        policy_sha256=_digest(raw["policy_sha256"]),
        selection_record_sha256=_digest(raw["selection_record_sha256"]),
    )


def _artifacts(value: object) -> tuple[ArtifactIdentity, ...]:
    artifacts: list[ArtifactIdentity] = []
    for item in _list(value, 2):
        raw = _object(item, ("name", "kind", "size", "sha256"))
        name = _string(raw["name"])
        if _member({"name": name, "size": 0, "sha256": "0" * 64}).name != PurePosixPath(name).name:
            raise ValueError("rehearsal artifact name is invalid")
        kind_value = _string(raw["kind"])
        if kind_value != "wheel" and kind_value != "sdist":
            raise ValueError("rehearsal artifact kind is invalid")
        kind: Literal["wheel", "sdist"] = "wheel" if kind_value == "wheel" else "sdist"
        artifacts.append(
            ArtifactIdentity(name=name, kind=kind, size=_integer(raw["size"], 1), sha256=_digest(raw["sha256"]))
        )
    if {artifact.kind for artifact in artifacts} != {"wheel", "sdist"} or len(
        {artifact.name for artifact in artifacts}
    ) != 2:
        raise ValueError("rehearsal artifacts must identify one wheel and one sdist")
    return tuple(sorted(artifacts, key=lambda item: item.name))


def _resources(value: object) -> ResourceLimits:
    raw = _object(value, ("processes", "memory_bytes", "cpu_seconds", "scratch_bytes", "output_bytes", "wall_seconds"))
    return ResourceLimits(
        processes=_integer(raw["processes"], 1),
        memory_bytes=_integer(raw["memory_bytes"], 1),
        cpu_seconds=_integer(raw["cpu_seconds"], 1),
        scratch_bytes=_integer(raw["scratch_bytes"], 1),
        output_bytes=_integer(raw["output_bytes"], 1),
        wall_seconds=_integer(raw["wall_seconds"], 1),
    )


def _time(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(_string(value))
    except ValueError:
        raise ValueError("rehearsal timestamp is invalid") from None
    if result.tzinfo is None:
        raise ValueError("rehearsal timestamp requires a timezone")
    return result


def _stream(value: object) -> StreamObservation:
    raw = _object(value, ("full_sha256", "total_bytes", "capture_limit_bytes", "truncated", "redacted", "retained"))
    stream = StreamObservation(
        full_sha256=_digest(raw["full_sha256"]),
        total_bytes=_integer(raw["total_bytes"]),
        capture_limit_bytes=_integer(raw["capture_limit_bytes"], 1),
        truncated=_boolean(raw["truncated"]),
        redacted=_boolean(raw["redacted"]),
        retained=_member(raw["retained"]),
    )
    if stream.retained.size > stream.capture_limit_bytes or stream.retained.size > stream.total_bytes:
        raise ValueError("rehearsal stream byte accounting is invalid")
    if (
        not stream.redacted
        and not stream.truncated
        and (stream.retained.size != stream.total_bytes or stream.retained.sha256 != stream.full_sha256)
    ):
        raise ValueError("rehearsal full stream does not match retained bytes")
    return stream


def _execution(value: object) -> ExecutionObservation:
    raw = _object(
        value,
        (
            "argv",
            "started_at",
            "finished_at",
            "resources",
            "process",
            "timeout",
            "policy",
            "teardown",
            "stdout",
            "stderr",
            "assertions",
            "behavioral_assertions",
        ),
    )
    process = _object(raw["process"], ("exit_code", "signal"))
    exit_code = None if process["exit_code"] is None else _integer(process["exit_code"])
    signal = None if process["signal"] is None else _integer(process["signal"], 1)
    if (
        (exit_code is not None and signal is not None)
        or (exit_code is not None and exit_code > 255)
        or (signal is not None and signal > 128)
    ):
        raise ValueError("rehearsal process outcome is invalid")
    timeout = _object(raw["timeout"], ("triggered", "deadline_seconds"))
    policy = _object(raw["policy"], ("verdict", "complete", "lost_events", "observation"))
    verdict_value = _string(policy["verdict"])
    if verdict_value not in ("allow", "deny", "unavailable"):
        raise ValueError("rehearsal policy verdict is invalid")
    verdict: Literal["allow", "deny", "unavailable"] = (
        "allow" if verdict_value == "allow" else "deny" if verdict_value == "deny" else "unavailable"
    )
    teardown = _object(raw["teardown"], ("result", "observation"))
    teardown_value = _string(teardown["result"])
    if teardown_value not in ("verified-empty", "failed", "unavailable"):
        raise ValueError("rehearsal teardown result is invalid")
    teardown_result: Literal["verified-empty", "failed", "unavailable"] = (
        "verified-empty"
        if teardown_value == "verified-empty"
        else "failed"
        if teardown_value == "failed"
        else "unavailable"
    )
    assertions: list[documentation_commands.TranscriptAssertion] = []
    for item in _list(raw["assertions"], 20):
        assertion = _object(item, ("stream", "contains"))
        stream_value = _string(assertion["stream"])
        if stream_value != "stdout" and stream_value != "stderr":
            raise ValueError("rehearsal assertion stream is invalid")
        stream: Literal["stdout", "stderr"] = "stdout" if stream_value == "stdout" else "stderr"
        assertions.append(
            documentation_commands.TranscriptAssertion(stream=stream, contains=_string(assertion["contains"]))
        )
    _list(raw["behavioral_assertions"], 0)
    result = ExecutionObservation(
        argv=tuple(_string(argument, 4096) for argument in _list(raw["argv"], 100)),
        started_at=_time(raw["started_at"]),
        finished_at=_time(raw["finished_at"]),
        resources=_resources(raw["resources"]),
        exit_code=exit_code,
        signal=signal,
        timed_out=_boolean(timeout["triggered"]),
        deadline_seconds=_integer(timeout["deadline_seconds"], 1),
        policy_verdict=verdict,
        policy_complete=_boolean(policy["complete"]),
        lost_events=_integer(policy["lost_events"]),
        policy_observation=_optional_member(policy["observation"]),
        teardown_result=teardown_result,
        teardown_observation=_optional_member(teardown["observation"]),
        stdout=_stream(raw["stdout"]),
        stderr=_stream(raw["stderr"]),
        assertions=tuple(assertions),
    )
    if (
        not result.argv
        or result.finished_at < result.started_at
        or result.deadline_seconds != result.resources.wall_seconds
    ):
        raise ValueError("rehearsal execution timing or plan is invalid")
    if (
        result.stdout.capture_limit_bytes != result.resources.output_bytes
        or result.stderr.capture_limit_bytes != result.resources.output_bytes
    ):
        raise ValueError("rehearsal output limit is not bound to its resource observation")
    return result


def _public(value: object, subject: InitialExportSubject) -> PublicBinding:
    raw = _object(value, ("repository", "repository_id", "commit_sha", "tree", "cutover_run"))
    run = _object(raw["cutover_run"], ("id", "attempt", "workflow_id", "name", "path", "event", "head_sha"))
    commit = _digest(raw["commit_sha"], 40)
    if (
        raw["repository"] != "mpeter/fieldkit-cli"
        or raw["tree"] != subject.exported_tree
        or run["head_sha"] != commit
        or run["name"] != "Cutover verification"
        or run["path"] != ".github/workflows/cutover.yml"
        or run["event"] != "push"
    ):
        raise ValueError("rehearsal public mapping is invalid")
    return PublicBinding(
        repository="mpeter/fieldkit-cli",
        repository_id=_integer(raw["repository_id"], 1),
        commit_sha=commit,
        tree=_digest(raw["tree"], 40),
        run_id=_integer(run["id"], 1),
        run_attempt=_integer(run["attempt"], 1),
        workflow_id=_integer(run["workflow_id"], 1),
    )


def _schema(data: bytes) -> dict[str, JsonValue]:
    schema = _json(data)
    stack: list[object] = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$dynamicRef" or (
                    key == "$ref" and (not isinstance(child, str) or not child.startswith("#/$defs/"))
                ):
                    raise ValueError("rehearsal schema references must be local definitions")
                stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    version = _object(_object(schema.get("properties")).get("schema_version"))
    if version.get("const") != 2 or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("rehearsal schema version is unsupported")
    return schema


def _schema_validate(receipt: dict[str, JsonValue], schema: dict[str, JsonValue]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        # An explicit empty registry uses referencing's fail-to-retrieve default,
        # not jsonschema's implicit remote-retrieval registry.
        registry: Registry[Schema] = Registry()
        error = next(
            Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).iter_errors(receipt), None
        )
    except (RecursionError, ValueError, SchemaError, Unresolvable):
        raise ValueError("rehearsal schema evaluation failed") from None
    if error is not None:
        raise ValueError("rehearsal schema validation failed")


def _members(value: object, supplied: Mapping[str, bytes]) -> dict[str, MemberIdentity]:
    if any(not isinstance(data, bytes) for data in supplied.values()):
        raise ValueError("rehearsal retained member bytes are invalid")
    if len(supplied) > MAX_RETAINED_MEMBERS or sum(len(data) for data in supplied.values()) > MAX_EVIDENCE_BYTES:
        raise ValueError("rehearsal retained members exceed limit")
    result: dict[str, MemberIdentity] = {}
    for item in _list(value, MAX_RETAINED_MEMBERS):
        member = _member(item)
        if member.name in result or member.name not in supplied:
            raise ValueError("rehearsal member catalog is invalid")
        data = supplied[member.name]
        if len(data) != member.size or sha256(data).hexdigest() != member.sha256:
            raise ValueError("rehearsal member digest or size does not match")
        result[member.name] = member
    if set(result) != set(supplied):
        raise ValueError("rehearsal member catalog is not closed")
    return result


def _require_member(member: MemberIdentity | None, catalog: Mapping[str, MemberIdentity]) -> None:
    if member is not None and catalog.get(member.name) != member:
        raise ValueError("rehearsal observation is not catalog-bound")


def _scenarios(
    value: object, phase: Phase, catalog: Mapping[str, MemberIdentity]
) -> tuple[tuple[ScenarioObservation, ...], tuple[str, ...]]:
    manual = {
        item.scenario_id: item
        for item in documentation_manual.MANUAL_SCENARIOS
        if (item.proof_type == "credentialed-integration") == (phase == "private-candidate")
    }
    outer = (
        {item.identifier: item for item in documentation_commands.OUTER_SCENARIOS} if phase == "public-release" else {}
    )
    expected = set(manual) | set(outer)
    if (len(manual), len(outer)) != ((34, 0) if phase == "private-candidate" else (13, 10)):
        raise ValueError("rehearsal registered mandatory coverage is invalid")
    result: list[ScenarioObservation] = []
    seen: set[str] = set()
    for item in _list(value, len(expected)):
        raw = _object(item, ("id", "precondition_id", "documented_block_sha256", "actor", "commands"))
        identifier = _string(raw["id"])
        if identifier in seen or identifier not in expected:
            raise ValueError("rehearsal scenario coverage is invalid")
        seen.add(identifier)
        manual_registration = manual.get(identifier)
        outer_registration = outer.get(identifier)
        registration = manual_registration if manual_registration is not None else outer_registration
        assert registration is not None
        block_digest = _digest(raw["documented_block_sha256"]) if raw["documented_block_sha256"] is not None else None
        if (
            raw["precondition_id"] != registration.precondition_id
            or raw["actor"] != registration.actor
            or block_digest != (manual_registration.sha256 if manual_registration is not None else None)
        ):
            raise ValueError("rehearsal scenario does not match its registration")
        commands = tuple(_execution(command) for command in _list(raw["commands"], 100))
        if not commands:
            raise ValueError("rehearsal scenario has no execution observation")
        for command in commands:
            for member in (
                command.stdout.retained,
                command.stderr.retained,
                command.policy_observation,
                command.teardown_observation,
            ):
                _require_member(member, catalog)
            if outer_registration is not None and (
                len(commands) != 1
                or command.argv != outer_registration.argv
                or command.assertions != outer_registration.assertions
            ):
                raise ValueError("rehearsal outer plan or assertions do not match registration")
            if manual_registration is not None and command.assertions:
                raise ValueError("rehearsal manual assertions have no registered verifier")
        result.append(
            ScenarioObservation(
                identifier=identifier,
                precondition_id=registration.precondition_id,
                block_sha256=block_digest,
                actor=registration.actor,
                commands=commands,
            )
        )
    return tuple(result), tuple(sorted(expected - seen))


def validate_rehearsal(
    receipt_bytes: bytes,
    *,
    schema_bytes: bytes,
    context: ProtectedRehearsalContext | None,
    retained_members: Mapping[str, bytes],
    artifact_bytes: Mapping[str, bytes],
) -> RehearsalValidation:
    """Decode observations without allowing hashes or transcripts to approve proof."""
    if context is not None and sha256(schema_bytes).hexdigest() != context.control.schema_sha256:
        raise ValueError("rehearsal schema does not match protected context")
    schema = _schema(schema_bytes)
    receipt = _json(receipt_bytes)
    phase_value = _string(receipt.get("phase"))
    if phase_value != "private-candidate" and phase_value != "public-release":
        raise ValueError("rehearsal phase is invalid")
    phase: Phase = "private-candidate" if phase_value == "private-candidate" else "public-release"
    keys = (
        "schema_version",
        "phase",
        "subject",
        "control",
        "environment",
        "artifacts",
        "scenarios",
        "review",
        "members",
    )
    _object(receipt, (*keys, "public", "private_receipt") if phase == "public-release" else keys)
    if receipt["schema_version"] != 2 or type(receipt["schema_version"]) is not int:
        raise ValueError("rehearsal schema version is unsupported")
    subject = _subject(receipt["subject"])
    if phase == "public-release" and isinstance(subject, PublicHistorySubject):
        raise ValueError("unregistered-successor-observer")
    if context is not None:
        _schema_validate(receipt, schema)
    control = _control(receipt["control"])
    if control.schema_sha256 != sha256(schema_bytes).hexdigest():
        raise ValueError("rehearsal schema identity does not match supplied bytes")
    environment = _object(receipt["environment"], ("os", "architecture", "python", "uv", "git", "make"))
    for value in environment.values():
        _string(value)
    review = _object(receipt["review"], ("reviewer", "reviewed_at", "immutable_url"))
    _string(review["reviewer"])
    _time(review["reviewed_at"])
    if not _string(review["immutable_url"], 2048).startswith("https://"):
        raise ValueError("rehearsal review reference is invalid")
    artifacts = _artifacts(receipt["artifacts"])
    if set(artifact_bytes) != {artifact.name for artifact in artifacts}:
        raise ValueError("rehearsal artifact catalog is not closed")
    for artifact in artifacts:
        data = artifact_bytes[artifact.name]
        if not isinstance(data, bytes) or len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("rehearsal artifact bytes exceed limit or are invalid")
        if len(data) != artifact.size or sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("rehearsal artifact digest or size does not match")
    catalog = _members(receipt["members"], retained_members)
    scenarios, missing = _scenarios(receipt["scenarios"], phase, catalog)
    public = (
        _public(receipt["public"], subject)
        if phase == "public-release" and isinstance(subject, InitialExportSubject)
        else None
    )
    private_receipt = _member(receipt["private_receipt"]) if phase == "public-release" else None
    linked_private: RehearsalValidation | None = None
    if private_receipt is not None:
        _require_member(private_receipt, catalog)
        private = _json(retained_members[private_receipt.name])
        if (
            private.get("phase") != "private-candidate"
            or _subject(private.get("subject")) != subject
            or _artifacts(private.get("artifacts")) != artifacts
        ):
            raise ValueError("rehearsal private and public subjects do not match")
        private_names = {_member(item).name for item in _list(private.get("members"), MAX_RETAINED_MEMBERS)}
        if not private_names <= set(retained_members):
            raise ValueError("rehearsal linked private member catalog is incomplete")
        linked_private = validate_rehearsal(
            retained_members[private_receipt.name],
            schema_bytes=schema_bytes,
            context=None,
            retained_members={name: retained_members[name] for name in private_names},
            artifact_bytes=artifact_bytes,
        )
    if context is not None and (
        phase != context.phase
        or subject != context.subject
        or control != context.control
        or artifacts != tuple(sorted(context.artifacts, key=lambda item: item.name))
        or public != context.public
        or (private_receipt.sha256 if private_receipt is not None else None) != context.private_receipt_sha256
    ):
        raise ValueError("rehearsal identities do not match protected context")
    issues = {"behavioral-verifiers-unapproved", "backend-unqualified"}
    if context is None:
        issues.add("controller-selection-unapproved")
    if missing:
        issues.add("scenario-coverage-incomplete")
    if linked_private is not None and linked_private.missing_scenarios:
        issues.add("linked-private-coverage-incomplete")
    for scenario in scenarios:
        for command in scenario.commands:
            if context is not None and command.resources != context.resources:
                raise ValueError("rehearsal resources do not match protected context")
            if command.exit_code != 0 or command.signal is not None:
                issues.add("execution-nonzero-or-unavailable")
            if command.timed_out:
                issues.add("controller-timeout")
            if (
                command.policy_verdict != "allow"
                or not command.policy_complete
                or command.lost_events
                or command.policy_observation is None
            ):
                issues.add("policy-observation-nonpassing")
            if command.teardown_result != "verified-empty" or command.teardown_observation is None:
                issues.add("teardown-unverified")
            if command.stdout.truncated or command.stderr.truncated:
                issues.add("evidence-truncated")
    return RehearsalValidation(
        phase=phase,
        subject=subject,
        control=control,
        artifacts=artifacts,
        scenarios=scenarios,
        public=public,
        pending_scenarios=tuple(sorted((*[item.identifier for item in scenarios], *missing))),
        missing_scenarios=missing,
        issues=tuple(sorted(issues)),
        schema_evaluation_performed=context is not None,
        linked_private=linked_private,
    )
