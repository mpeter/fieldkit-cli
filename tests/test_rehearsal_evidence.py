"""Pure phase-contract controls, not execution or backend qualification."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import (
    check_documentation_contract,
    check_documentation_examples,
    documentation_commands,
    documentation_manual,
)
from scripts import rehearsal_evidence as evidence
from scripts.documentation_manual import MANUAL_SCENARIOS
from tests import rehearsal_support

pytestmark = pytest.mark.unit


def test_manual_catalog_has_one_canonical_home_and_preserves_required_counts() -> None:
    """Manual identities and validation live separately from executable argv."""
    assert documentation_manual.ManualScenario.__module__ == "scripts.documentation_manual"
    assert documentation_manual.manual_scenarios.__module__ == "scripts.documentation_manual"
    assert check_documentation_contract.manual_scenarios is documentation_manual.manual_scenarios
    assert check_documentation_examples.manual_scenarios is documentation_manual.manual_scenarios
    assert evidence.documentation_manual is documentation_manual
    assert len(documentation_manual.MANUAL_SCENARIOS) == 47
    assert sum(item.proof_type == "credentialed-integration" for item in MANUAL_SCENARIOS) == 34
    assert sum(item.proof_type == "release-cutover" for item in MANUAL_SCENARIOS) == 13
    assert len(documentation_commands.OUTER_SCENARIOS) == 10
    assert all(item.behavioral_verifier is None for item in MANUAL_SCENARIOS)
    assert not any(
        hasattr(documentation_commands, name) for name in ("ManualScenario", "MANUAL_SCENARIOS", "manual_scenarios")
    )


@pytest.mark.parametrize("phase", ["private-candidate", "public-release"])
def test_schema_accepts_phase_structure_without_implying_proof(phase: str) -> None:
    receipt, _ = rehearsal_support.fixture_receipt(phase)
    validator = Draft202012Validator(json.loads(rehearsal_support.schema_bytes()))

    assert validator.is_valid(json.loads(json.dumps(receipt)))


def test_schema_one_is_incompatible() -> None:
    receipt = {
        "schema_version": 1,
        "subject": {
            "source_sha": "a" * 40,
            "documentation_contract_sha256": "b" * 64,
            "clean_export_tree": "c" * 40,
            "repository": "mpeter/fieldkit-cli",
            "public_commit_sha": "d" * 40,
            "workflow_name": "Cutover verification",
            "workflow_path": ".github/workflows/cutover.yml",
            "workflow_event": "push",
            "workflow_run_id": 1,
            "workflow_run_attempt": 1,
        },
        "environment": dict.fromkeys(("os", "architecture", "python", "uv", "git", "make"), "fixture"),
        "artifacts": [{"name": "wheel", "sha256": "e" * 64}, {"name": "sdist", "sha256": "f" * 64}],
        "verified_blocks": ["fixture-block"],
        "scenarios": [
            {
                "id": "fixture-block",
                "actor": "maintainer",
                "proof_type": "credentialed-integration",
                "documented_block_sha256": "1" * 64,
                "status": "pass",
                "started_at": "2026-09-29T12:00:00Z",
                "duration_seconds": 1,
                "verified_blocks": ["fixture-block"],
                "commands": [
                    {
                        "argv": ["fieldkit", "--help"],
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "stdout_sha256": sha256(b"").hexdigest(),
                        "stderr_sha256": sha256(b"").hexdigest(),
                        "assertions": [],
                    }
                ],
            }
        ],
        "review": {
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-09-29T12:00:00Z",
            "immutable_url": "https://example.com/reviews/fixture",
        },
    }

    assert not Draft202012Validator(json.loads(rehearsal_support.schema_bytes())).is_valid(receipt)


def artifacts() -> dict[str, bytes]:
    return {"fieldkit_cli-1.0.0-py3-none-any.whl": b"w", "fieldkit_cli-1.0.0.tar.gz": b"s"}


def protected(receipt: dict[str, object], members: dict[str, bytes]) -> evidence.ProtectedRehearsalContext:
    # Matching fixture identities do not authenticate a caller or approve proof.
    diagnostic = rehearsal_support.validate(receipt, members)
    return evidence.ProtectedRehearsalContext(
        phase=diagnostic.phase,
        subject=diagnostic.subject,
        control=diagnostic.control,
        artifacts=diagnostic.artifacts,
        resources=diagnostic.scenarios[0].commands[0].resources,
        public=diagnostic.public,
        private_receipt_sha256=str(rehearsal_support.mapping(receipt["private_receipt"])["sha256"])
        if "private_receipt" in receipt
        else None,
    )


def command(receipt: dict[str, object]) -> dict[str, object]:
    return rehearsal_support.mapping(
        rehearsal_support.items(
            rehearsal_support.mapping(rehearsal_support.items(receipt["scenarios"])[0])["commands"]
        )[0]
    )


def successor_receipt(phase: str = "private-candidate") -> tuple[dict[str, object], dict[str, bytes]]:
    receipt, members = rehearsal_support.fixture_receipt(phase)
    receipt["subject"] = {
        "source_kind": "public-history",
        "repository": "mpeter/fieldkit-cli",
        "repository_id": 1,
        "source_commit": "7" * 40,
        "source_tree": "c" * 40,
        "version": "1.0.1",
        "planned_tag": "v1.0.1",
        "source_sha256": "8" * 64,
        "initial_commit": "9" * 40,
        "initial_tree": "a" * 40,
        "record_sha256": "b" * 64,
        "documentation_contract_sha256": "d" * 64,
    }
    for artifact in rehearsal_support.items(receipt["artifacts"]):
        row = rehearsal_support.mapping(artifact)
        row["name"] = str(row["name"]).replace("1.0.0", "1.0.1")
    if phase == "public-release":
        private, _ = successor_receipt()
        data = json.dumps(private).encode()
        members["evidence/private-rehearsal.json"] = data
        receipt["private_receipt"] = rehearsal_support.descriptor("evidence/private-rehearsal.json", data)
        receipt["members"] = [rehearsal_support.descriptor(name, value) for name, value in members.items()]
    return receipt, members


def test_successor_subject_preserves_current_vs_historical_identity() -> None:
    receipt, members = successor_receipt()

    result = rehearsal_support.validate(receipt, members)

    assert result.passing is False
    assert isinstance(result.subject, evidence.PublicHistorySubject)
    assert result.subject.source_commit != result.subject.initial_commit
    assert result.subject.source_tree != result.subject.initial_tree
    assert result.subject.version == "1.0.1"
    assert not hasattr(result.subject, "private_source_sha")
    assert Draft202012Validator(json.loads(rehearsal_support.schema_bytes())).is_valid(json.loads(json.dumps(receipt)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_kind", "initial-export"),
        ("private_source_sha", "0" * 40),
        ("initial_anchor_sha256", "0" * 64),
        ("version", "1.0.0"),
        ("planned_tag", "v1.0.0"),
    ],
)
def test_successor_wrong_kind_or_mixed_fields_fail_closed(field: str, value: str) -> None:
    receipt, members = successor_receipt()
    rehearsal_support.mapping(receipt["subject"])[field] = value

    with pytest.raises(ValueError, match=r"rehearsal (object|source|history)"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("field", ["commit_sha", "tree"])
def test_successor_historical_values_do_not_enable_initial_observer(field: str) -> None:
    receipt, members = successor_receipt("public-release")
    public = rehearsal_support.mapping(receipt["public"])
    subject = rehearsal_support.mapping(receipt["subject"])
    public[field] = subject["initial_commit" if field == "commit_sha" else "initial_tree"]
    if field == "commit_sha":
        rehearsal_support.mapping(public["cutover_run"])["head_sha"] = public[field]

    with pytest.raises(ValueError, match="unregistered-successor-observer"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("selected", [False, True])
def test_successor_public_observer_is_unregistered(selected: bool) -> None:
    receipt, members = successor_receipt("public-release")
    private, private_members = successor_receipt()
    private_result = rehearsal_support.validate(private, private_members)
    public = evidence.PublicBinding(
        repository="mpeter/fieldkit-cli",
        repository_id=1,
        commit_sha="7" * 40,
        tree="c" * 40,
        run_id=1,
        run_attempt=1,
        workflow_id=1,
    )
    context = (
        evidence.ProtectedRehearsalContext(
            phase="public-release",
            subject=private_result.subject,
            control=private_result.control,
            artifacts=private_result.artifacts,
            resources=private_result.scenarios[0].commands[0].resources,
            public=public,
            private_receipt_sha256=str(rehearsal_support.mapping(receipt["private_receipt"])["sha256"]),
        )
        if selected
        else None
    )

    with pytest.raises(ValueError, match="unregistered-successor-observer"):
        rehearsal_support.validate(receipt, members, context=context)

    assert not Draft202012Validator(json.loads(rehearsal_support.schema_bytes())).is_valid(
        json.loads(json.dumps(receipt))
    )


@pytest.mark.parametrize("phase,required", [("private-candidate", 34), ("public-release", 23)])
@pytest.mark.parametrize("selected", [False, True])
def test_complete_claims_and_fake_good_context_never_approve(phase: str, required: int, selected: bool) -> None:
    receipt, members = rehearsal_support.fixture_receipt(phase)
    context = protected(receipt, members) if selected else None

    result = rehearsal_support.validate(receipt, members, context=context)

    assert result.passing is False
    assert len(result.pending_scenarios) == required
    assert result.verified_blocks == ()
    assert result.missing_scenarios == ()
    assert {"behavioral-verifiers-unapproved", "backend-unqualified"} <= set(result.issues)
    assert result.schema_evaluation_performed is selected
    assert ("controller-selection-unapproved" in result.issues) is (not selected)
    if phase == "public-release":
        assert result.linked_private is not None
        assert len(result.linked_private.pending_scenarios) == 34
        assert result.public is not None
        assert isinstance(result.subject, evidence.InitialExportSubject)
        assert result.subject.private_source_sha != result.public.commit_sha


def test_partial_diagnostic_does_not_reduce_mandatory_coverage() -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    receipt["scenarios"] = []

    result = rehearsal_support.validate(receipt, members)

    assert result.passing is False
    assert len(result.missing_scenarios) == len(result.pending_scenarios) == 34
    assert "scenario-coverage-incomplete" in result.issues


@pytest.mark.parametrize(
    "field,value,issue",
    [
        ("process", {"exit_code": 124, "signal": None}, "execution-nonzero-or-unavailable"),
        ("process", {"exit_code": None, "signal": 9}, "execution-nonzero-or-unavailable"),
        ("timeout", {"triggered": True, "deadline_seconds": 60}, "controller-timeout"),
        (
            "policy",
            {"verdict": "allow", "complete": False, "lost_events": 1, "observation": None},
            "policy-observation-nonpassing",
        ),
        ("teardown", {"result": "unavailable", "observation": None}, "teardown-unverified"),
    ],
)
def test_execution_failure_dimensions_remain_separate(field: str, value: object, issue: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    command(receipt)[field] = value

    result = rehearsal_support.validate(receipt, members)

    assert result.passing is False
    assert issue in result.issues
    assert ("controller-timeout" in result.issues) is (field == "timeout")


def test_truncation_is_not_successful_full_stream() -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    stdout = rehearsal_support.mapping(command(receipt)["stdout"])
    stdout.update(truncated=True, total_bytes=1, full_sha256=sha256(b"x").hexdigest())

    result = rehearsal_support.validate(receipt, members)

    assert result.passing is False
    assert "evidence-truncated" in result.issues
    assert result.scenarios[0].commands[0].stdout.total_bytes == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "unregistered-scenario"),
        ("precondition_id", "invented-precondition"),
        ("actor", "external-user"),
        ("documented_block_sha256", "0" * 64),
    ],
)
def test_manual_registration_substitutions_rejected(field: str, value: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    rehearsal_support.mapping(rehearsal_support.items(receipt["scenarios"])[0])[field] = value

    with pytest.raises(ValueError, match="rehearsal scenario"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("mutation", ["delete", "add", "stream", "argv"])
def test_outer_exact_plan_and_assertions_cannot_be_candidate_selected(mutation: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt("public-release")
    outer = rehearsal_support.mapping(rehearsal_support.items(receipt["scenarios"])[-1])
    observed = rehearsal_support.mapping(rehearsal_support.items(outer["commands"])[0])
    assertions = rehearsal_support.items(observed["assertions"])
    if mutation == "delete":
        observed["assertions"] = []
    elif mutation == "add":
        assertions.append({"stream": "stdout", "contains": "forged"})
    elif mutation == "stream":
        rehearsal_support.mapping(assertions[0])["stream"] = "stderr"
    else:
        observed["argv"] = ["fieldkit", "--help"]

    with pytest.raises(ValueError, match="outer plan or assertions"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("mutation", ["extra", "missing", "changed", "duplicate", "traversal"])
def test_closed_member_catalog_recomputed(mutation: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    if mutation == "extra":
        members["extra.txt"] = b"extra"
    elif mutation == "missing":
        members.clear()
    elif mutation == "changed":
        members["evidence/empty.txt"] = b"changed"
    elif mutation == "duplicate":
        rehearsal_support.items(receipt["members"]).append(rehearsal_support.items(receipt["members"])[0])
    else:
        rehearsal_support.mapping(rehearsal_support.items(receipt["members"])[0])["name"] = "../outside.txt"

    with pytest.raises(ValueError, match="rehearsal member"):
        rehearsal_support.validate(receipt, members)


def test_artifact_bytes_are_not_receipt_self_assertion() -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    changed = artifacts()
    changed[next(iter(changed))] = b"changed"

    with pytest.raises(ValueError, match="artifact digest or size"):
        evidence.validate_rehearsal(
            json.dumps(receipt).encode(),
            schema_bytes=rehearsal_support.schema_bytes(),
            context=None,
            retained_members=members,
            artifact_bytes=changed,
        )


@pytest.mark.parametrize("binding", ["subject", "control", "public", "resources", "private_receipt"])
def test_protected_expectations_not_selected_by_receipt(binding: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt("public-release")
    context = protected(receipt, members)
    if binding == "subject":
        assert isinstance(context.subject, evidence.InitialExportSubject)
        context = replace(context, subject=replace(context.subject, private_source_sha="0" * 40))
    elif binding == "control":
        context = replace(context, control=replace(context.control, controller_revision="0" * 40))
    elif binding == "public":
        assert context.public is not None
        context = replace(context, public=replace(context.public, run_attempt=2))
    elif binding == "resources":
        context = replace(context, resources=replace(context.resources, processes=1))
    else:
        context = replace(context, private_receipt_sha256="0" * 64)

    with pytest.raises(ValueError, match="protected context"):
        rehearsal_support.validate(receipt, members, context=context)


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize(
    "reference", ["https://example.com/schema.json", "file:///outside/schema.json", "#/properties/scenarios"]
)
def test_schema_refs_rejected_before_any_evaluation_or_network(
    monkeypatch: pytest.MonkeyPatch, selected: bool, reference: str
) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    context = protected(receipt, members) if selected else None
    schema = json.loads(rehearsal_support.schema_bytes())
    schema["$ref"] = reference
    supplied = json.dumps(schema).encode()
    rehearsal_support.mapping(receipt["control"])["schema_sha256"] = sha256(supplied).hexdigest()
    if context is not None:
        context = replace(context, control=replace(context.control, schema_sha256=sha256(supplied).hexdigest()))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("untrusted schema attempted evaluation or network")

    monkeypatch.setattr(evidence, "Draft202012Validator", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    with pytest.raises(ValueError, match="local definitions"):
        rehearsal_support.validate(receipt, members, context=context, schema=supplied)


def test_diagnostic_schema_pattern_cannot_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    schema = json.loads(rehearsal_support.schema_bytes())
    schema["properties"]["phase"]["pattern"] = "(a+)+$"
    supplied = json.dumps(schema).encode()
    rehearsal_support.mapping(receipt["control"])["schema_sha256"] = sha256(supplied).hexdigest()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("diagnostic schema evaluation attempted")

    monkeypatch.setattr(evidence, "Draft202012Validator", forbidden)
    result = rehearsal_support.validate(receipt, members, schema=supplied)

    assert result.passing is False
    assert result.schema_evaluation_performed is False
    assert "controller-selection-unapproved" in result.issues


def test_schema_digest_checked_before_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    context = protected(receipt, members)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("wrong schema evaluated before protected binding")

    monkeypatch.setattr(evidence, "Draft202012Validator", forbidden)
    with pytest.raises(ValueError, match="schema does not match protected context"):
        rehearsal_support.validate(receipt, members, context=context, schema=b"{}")


@pytest.mark.parametrize(
    "payload", [b'{"private@example.com":1,"private@example.com":2}', b"\xff", b"[" * 66 + b"]" * 66]
)
def test_canonical_json_errors_are_sanitized(payload: bytes) -> None:
    with pytest.raises(ValueError) as caught:
        evidence.validate_rehearsal(
            payload,
            schema_bytes=rehearsal_support.schema_bytes(),
            context=None,
            retained_members={},
            artifact_bytes=artifacts(),
        )

    assert "private@example.com" not in str(caught.value)
    assert "outside" not in str(caught.value)


@pytest.mark.parametrize("target", ["receipt", "schema", "members", "artifact"])
def test_input_limits_before_evaluation(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    monkeypatch.setattr(evidence, "MAX_EVIDENCE_BYTES", 1 if target != "artifact" else 5000000)
    monkeypatch.setattr(evidence, "MAX_ARTIFACT_BYTES", 0)
    supplied_receipt = json.dumps(receipt).encode()
    supplied_schema = rehearsal_support.schema_bytes()
    if target == "schema":
        supplied_receipt = b"{}"
    elif target == "members":
        monkeypatch.setattr(evidence, "MAX_EVIDENCE_BYTES", max(len(supplied_receipt), len(supplied_schema)))
        members["large.txt"] = b"x" * (evidence.MAX_EVIDENCE_BYTES + 1)

    with pytest.raises(ValueError, match="limit"):
        evidence.validate_rehearsal(
            supplied_receipt,
            schema_bytes=supplied_schema,
            context=None,
            retained_members=members,
            artifact_bytes=artifacts(),
        )


def test_deleted_registry_cannot_reduce_fixed_required_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", MANUAL_SCENARIOS[1:])

    with pytest.raises(ValueError, match="registered mandatory coverage"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("field", ["full_sha256", "retained"])
def test_stream_bytes_or_observation_reference_cannot_be_substituted(field: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    stdout = rehearsal_support.mapping(command(receipt)["stdout"])
    if field == "full_sha256":
        stdout[field] = "0" * 64
    else:
        rehearsal_support.mapping(command(receipt)["policy"])["observation"] = rehearsal_support.descriptor(
            "missing.txt", b""
        )

    with pytest.raises(ValueError, match=r"rehearsal (full stream|observation)"):
        rehearsal_support.validate(receipt, members)


@pytest.mark.parametrize("mutation", ["subject", "coverage", "unknown", "schema"])
def test_linked_private_receipt_is_decoded_not_just_hashed(mutation: str) -> None:
    receipt, members = rehearsal_support.fixture_receipt("public-release")
    private = rehearsal_support.mapping(json.loads(members["evidence/private-rehearsal.json"]))
    if mutation == "subject":
        rehearsal_support.mapping(private["subject"])["private_source_sha"] = "0" * 40
    elif mutation == "coverage":
        private["scenarios"] = []
    elif mutation == "unknown":
        rehearsal_support.mapping(rehearsal_support.items(private["scenarios"])[0])["id"] = "unregistered"
    else:
        private["schema_version"] = 1
    data = json.dumps(private).encode()
    members["evidence/private-rehearsal.json"] = data
    receipt["private_receipt"] = rehearsal_support.descriptor("evidence/private-rehearsal.json", data)
    receipt["members"] = [rehearsal_support.descriptor(name, value) for name, value in members.items()]

    if mutation == "coverage":
        result = rehearsal_support.validate(receipt, members)
        assert result.passing is False
        assert "linked-private-coverage-incomplete" in result.issues
        assert result.linked_private is not None
        assert len(result.linked_private.pending_scenarios) == 34
    else:
        with pytest.raises(ValueError, match="rehearsal"):
            rehearsal_support.validate(receipt, members)


def test_trusted_schema_local_missing_ref_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    context = protected(receipt, members)
    schema = json.loads(rehearsal_support.schema_bytes())
    schema["$ref"] = "#/$defs/not-present"
    supplied = json.dumps(schema).encode()
    rehearsal_support.mapping(receipt["control"])["schema_sha256"] = sha256(supplied).hexdigest()
    context = replace(context, control=replace(context.control, schema_sha256=sha256(supplied).hexdigest()))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("local reference attempted network retrieval")

    monkeypatch.setattr("socket.create_connection", forbidden)
    with pytest.raises(ValueError, match="schema evaluation failed"):
        rehearsal_support.validate(receipt, members, context=context, schema=supplied)


def test_dynamic_ref_rejected_in_diagnostic_mode() -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    schema = json.loads(rehearsal_support.schema_bytes())
    schema["$dynamicRef"] = "#/$defs/subject"
    supplied = json.dumps(schema).encode()

    with pytest.raises(ValueError, match="local definitions"):
        rehearsal_support.validate(receipt, members, schema=supplied)


def test_artifact_acquisition_cap_is_separate_from_json_limit() -> None:
    receipt, members = rehearsal_support.fixture_receipt()
    supplied = artifacts()
    name = next(iter(supplied))
    supplied[name] = b"x" * (evidence.MAX_EVIDENCE_BYTES + 1)
    row = rehearsal_support.mapping(rehearsal_support.items(receipt["artifacts"])[0])
    row.update(size=len(supplied[name]), sha256=sha256(supplied[name]).hexdigest())

    result = evidence.validate_rehearsal(
        json.dumps(receipt).encode(),
        schema_bytes=rehearsal_support.schema_bytes(),
        context=None,
        retained_members=members,
        artifact_bytes=supplied,
    )

    assert result.passing is False
    assert max(item.size for item in result.artifacts) > evidence.MAX_EVIDENCE_BYTES


def test_consumer_download_uses_shared_artifact_cap_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import artifact_limits, release_consumer

    assert artifact_limits.MAX_ARTIFACT_BYTES == 100 * 1024 * 1024
    monkeypatch.setattr(release_consumer, "MAX_ARTIFACT_BYTES", 1)
    expected = release_consumer.ExpectedArtifact("wheel", "fixture.whl", sha256(b"xx").hexdigest())
    observed = release_consumer.ObservedArtifact(expected.name, expected.sha256, "https://example.com/fixture.whl")

    with pytest.raises(ValueError, match="exceeds the size limit"):
        release_consumer.download_verified(observed, expected, tmp_path, fetch=lambda _url, _timeout: b"xx")

    assert list(tmp_path.iterdir()) == []
