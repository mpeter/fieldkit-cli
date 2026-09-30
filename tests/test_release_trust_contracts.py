"""Structural contracts never authenticate a controller or approve publication."""

import copy
import json
from pathlib import Path
from typing import Literal

import pytest
from jsonschema import Draft202012Validator

from scripts.release_trust_contracts import MAX_CONTRACT_BYTES, validate_contract

pytestmark = pytest.mark.unit
SCHEMA_ROOT = Path(__file__).parents[1] / "docs/release-readiness"
DIGEST = "a" * 64
OID = "b" * 40


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def schemas() -> dict[str, dict[str, object]]:
    return {
        name: mapping(json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")))
        for name in ("release-trust-selection", "release-controller-receipt")
    }


def is_valid(name: str, fixture: dict[str, object]) -> bool:
    kind: Literal["selection", "receipt"] = "selection" if name == "release-trust-selection" else "receipt"
    try:
        result = validate_contract(
            json.dumps(fixture).encode("utf-8"), kind=kind, controller_root=SCHEMA_ROOT.parents[1]
        )
    except ValueError:
        return False
    assert result is None
    return True


def fixture_selection() -> dict[str, object]:
    identity = {
        "repository": "example/release-controller",
        "repository_id": 101,
        "owner_id": 102,
        "workflow_id": 103,
        "run_id": 104,
        "run_attempt": 1,
        "head_repository_id": 101,
        "workflow_path": ".github/workflows/fixture.yml",
        "workflow_ref": "example/release-controller/.github/workflows/fixture.yml@refs/heads/main",
        "workflow_blob_oid": OID,
        "workflow_blob_sha256": DIGEST,
        "event": "workflow_dispatch",
        "head_sha": OID,
    }
    producer = {
        **identity,
        "artifact_id": 105,
        "artifact_name": "fixture-candidate",
        "artifact_api_digest": f"sha256:{DIGEST}",
    }
    artifacts = [
        {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "kind": "wheel", "sha256": DIGEST},
        {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": DIGEST},
    ]
    return {
        "schema_version": 1,
        "kind": "fieldkit.release-trust-selection",
        "selection_id": "fictional-selection",
        "controller": {
            "repository": "example/release-controller",
            "repository_id": 101,
            "owner_id": 102,
            "source_commit": OID,
            "source_tree": OID,
            "manifest_sha256": DIGEST,
            "entrypoint": "controller/main.py",
            "members": [
                {"path": "controller/main.py", "size_bytes": 10, "sha256": DIGEST, "role": "fixture-entrypoint"}
            ],
        },
        "toolchain": {
            **{
                name: {"version": "fixture-version", "executable_sha256": DIGEST}
                for name in ("python", "uv", "git", "ssh_keygen")
            },
            "runtime_manifest_sha256": DIGEST,
            "uv_lock_sha256": DIGEST,
        },
        "policy": dict.fromkeys(
            (
                "public_tree_policy_sha256",
                "documentation_contract_sha256",
                "receipt_schema_sha256",
                "selection_schema_sha256",
            ),
            DIGEST,
        ),
        "expected_candidate": {
            "repository": "mpeter/fieldkit-cli",
            "repository_id": 201,
            **dict.fromkeys(
                ("private_source_sha", "private_source_tree", "exported_tree", "public_commit", "public_tree"), OID
            ),
            "version": "1.0.0",
            "planned_tag": "v1.0.0",
            "package": "fieldkit-cli",
            "artifacts": artifacts,
            "documentation_contract_sha256": DIGEST,
        },
        "producer": producer,
        "approval": {
            **identity,
            "environment_id": 106,
            "environment_name": "fixture-approval",
            "permitted_reviewer_ids": [107],
            "expected_producer": producer,
            "expected_manifest_sha256": DIGEST,
        },
        "signer": {
            "principal": "fixture-release",
            "public_key": "ssh-ed25519 ZmljdGlvbmFsLWtleQ==",
            "fingerprint_sha256": "SHA256:" + "A" * 43,
            "receipt_namespace": "fieldkit-release-receipt-v1",
            "tag_namespace": "git",
            "valid_after": "2026-01-01T00:00:00Z",
            "valid_before": "2027-01-01T00:00:00Z",
        },
        "revocation": {
            "format": "openssh-krl",
            "sha256": DIGEST,
            "generation": 1,
            "effective_at": "2026-01-01T00:00:00Z",
            "refresh_required_by": "2026-01-02T00:00:00Z",
        },
    }


def fixture_receipt() -> dict[str, object]:
    selection = fixture_selection()
    controller = mapping(selection["controller"])
    producer = mapping(selection["producer"])
    approval = mapping(selection["approval"])
    observed_approval = {
        key: value
        for key, value in approval.items()
        if key not in ("permitted_reviewer_ids", "expected_producer", "expected_manifest_sha256")
    }
    return {
        "schema_version": 1,
        "kind": "fieldkit.release-controller-receipt",
        "decision": "approved",
        "selection_sha256": DIGEST,
        "issued_at": "2026-01-01T12:00:00Z",
        "controller": {key: value for key, value in controller.items() if key not in ("members", "entrypoint")},
        "toolchain": selection["toolchain"],
        "candidate": selection["expected_candidate"],
        "producer": {
            **producer,
            "status": "completed",
            "conclusion": "success",
            "download_sha256": DIGEST,
            "approval_manifest_sha256": DIGEST,
        },
        "approval": {
            **observed_approval,
            "observed_reviewer_ids": [107],
            "job_id": 108,
            "job_name": "fixture-approval",
            "artifact_id": 109,
            "artifact_api_digest": f"sha256:{DIGEST}",
            "download_sha256": DIGEST,
            "matching_manifest_sha256": DIGEST,
            "status": "completed",
            "conclusion": "success",
        },
        "evidence": {
            "cutover_record_sha256": DIGEST,
            "ledger_sha256": DIGEST,
            "artifacts": mapping(selection["expected_candidate"])["artifacts"],
        },
        "signer": selection["signer"],
        "revocation": selection["revocation"],
        "checks": dict.fromkeys(
            (
                "C0_closure",
                "producer",
                "approval",
                "expected_identities",
                "structural_input",
                "complete_rehearsals",
                "artifacts",
            ),
            "pass",
        ),
    }


@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
def test_schema_and_fictional_fixture_are_structurally_valid(name: str) -> None:
    schema = schemas()[name]
    assert Draft202012Validator.check_schema(schema) is None
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    assert is_valid(name, fixture)
    assert "structur" in str(schema["description"]).lower()
    assert "authenticat" in str(schema["description"]).lower()


@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
@pytest.mark.parametrize(
    "key", ["schema_version", "kind", "controller", "producer", "approval", "signer", "revocation"]
)
def test_required_top_level_field_rejected(name: str, key: str) -> None:
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    changed = copy.deepcopy(fixture)
    del changed[key]
    assert not is_valid(name, changed)


@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
@pytest.mark.parametrize("key", ["receipt_selfdigest", "signature_sha256", "final_tag_identity"])
def test_circular_and_unknown_receipt_fields_rejected(name: str, key: str) -> None:
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    assert not is_valid(name, {**fixture, key: DIGEST})


@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
@pytest.mark.parametrize("section", ["controller", "producer", "approval", "toolchain", "signer", "revocation"])
def test_nested_objects_are_closed_and_all_fields_required(name: str, section: str) -> None:
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    nested = mapping(fixture[section])
    first_key = next(iter(nested))
    del nested[first_key]
    assert not is_valid(name, fixture)
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    mapping(fixture[section])["unknown"] = "unexpected"
    assert not is_valid(name, fixture)


@pytest.mark.parametrize(
    "key", ["repository_id", "owner_id", "workflow_id", "run_id", "run_attempt", "head_repository_id", "artifact_id"]
)
@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
def test_producer_numeric_ids_reject_booleans(name: str, key: str) -> None:
    fixture = fixture_selection() if name == "release-trust-selection" else fixture_receipt()
    mapping(fixture["producer"])[key] = True
    assert not is_valid(name, fixture)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("controller", "repository_id", True),
        ("producer", "run_id", True),
        ("controller", "source_commit", "a" * 39),
        ("controller", "manifest_sha256", "A" * 64),
        ("controller", "entrypoint", "../escape.py"),
        ("controller", "entrypoint", "/absolute.py"),
        ("controller", "entrypoint", "a//b"),
        ("controller", "entrypoint", "a.py\n"),
        ("controller", "entrypoint", "a/./b"),
        ("controller", "entrypoint", "a/../b"),
        ("controller", "entrypoint", "a\\b"),
        ("controller", "entrypoint", "caf\u00e9.py"),
        ("controller", "unexpected", 1),
        ("expected_candidate", "planned_tag", "v2.0.0"),
        ("expected_candidate", "package", "fieldkit"),
        ("signer", "receipt_namespace", "git"),
        ("signer", "tag_namespace", "other"),
        ("signer", "valid_after", "2026-02-30T00:00:00Z"),
        ("signer", "valid_after", "2026-01-01T00:00:00+00:00"),
        ("producer", "artifact_api_digest", DIGEST),
        ("revocation", "generation", True),
    ],
)
def test_invalid_selection_fields(section: str, key: str, value: object) -> None:
    fixture = fixture_selection()
    mapping(fixture[section])[key] = value
    assert not is_valid("release-trust-selection", fixture)


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [{"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": DIGEST}] * 2,
        [
            {"name": "wrong.whl", "kind": "wheel", "sha256": DIGEST},
            {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": DIGEST},
        ],
    ],
)
def test_artifact_set_must_contain_exact_release_wheel_and_sdist(artifacts: object) -> None:
    fixture = fixture_selection()
    mapping(fixture["expected_candidate"])["artifacts"] = artifacts
    assert not is_valid("release-trust-selection", fixture)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("producer", "status", "in_progress"),
        ("approval", "conclusion", "failure"),
        ("approval", "environment_id", True),
        ("approval", "observed_reviewer_ids", [True]),
        ("checks", "approval", "fail"),
        ("controller", "unknown", DIGEST),
    ],
)
def test_invalid_receipt_observations(section: str, key: str, value: object) -> None:
    fixture = fixture_receipt()
    mapping(fixture[section])[key] = value
    assert not is_valid("release-controller-receipt", fixture)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("producer", "workflow_ref"),
        ("producer", "event"),
        ("producer", "artifact_name"),
        ("approval", "workflow_ref"),
        ("approval", "event"),
        ("approval", "environment_name"),
        ("approval", "job_name"),
    ],
)
def test_receipt_identity_text_rejects_trailing_newline(section: str, key: str) -> None:
    fixture = fixture_receipt()
    observation = mapping(fixture[section])
    observation[key] = str(observation[key]) + "\n"
    assert not is_valid("release-controller-receipt", fixture)


def test_valid_structure_does_not_compare_identity_or_authenticate_signature() -> None:
    fixture = fixture_receipt()
    mapping(fixture["producer"])["repository_id"] = 999
    assert is_valid("release-controller-receipt", fixture)
    assert "signature" not in fixture


@pytest.mark.parametrize(
    "data",
    [
        b'{"duplicate": 1, "duplicate": 2}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": 1e999}',
        b"\xff",
        b"[]",
        b" " * (MAX_CONTRACT_BYTES + 1),
    ],
)
def test_strict_bounded_json_rejected(data: bytes) -> None:
    with pytest.raises(ValueError, match=r"JSON|object|byte limit"):
        validate_contract(data, kind="selection", controller_root=SCHEMA_ROOT.parents[1])


def test_unknown_schema_reference_has_no_remote_retrieval(tmp_path: Path) -> None:
    for name, schema in schemas().items():
        destination = tmp_path / "docs/release-readiness" / f"{name}.schema.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if name == "release-trust-selection":
            mapping(schema["properties"])["controller"] = {"$ref": "https://example.com/unregistered.schema.json"}
        destination.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ValueError, match="schema is invalid or unresolved"):
        validate_contract(json.dumps(fixture_selection()).encode(), kind="selection", controller_root=tmp_path)


def test_schema_symlink_is_not_followed(tmp_path: Path) -> None:
    (tmp_path / "docs").symlink_to(SCHEMA_ROOT.parent, target_is_directory=True)
    with pytest.raises(ValueError, match=r"unavailable|real directory"):
        validate_contract(json.dumps(fixture_selection()).encode(), kind="selection", controller_root=tmp_path)


@pytest.mark.parametrize("name", ["release-trust-selection", "release-controller-receipt"])
@pytest.mark.parametrize("dialect", [None, "https://example.com/fictional-private-marker"])
def test_schema_dialect_rejected_without_reflecting_content(tmp_path: Path, name: str, dialect: str | None) -> None:
    for schema_name, schema in schemas().items():
        destination = tmp_path / "docs/release-readiness" / f"{schema_name}.schema.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if schema_name == name:
            schema["description"] = "fictional-private-marker"
            if dialect is None:
                del schema["$schema"]
            else:
                schema["$schema"] = dialect
        destination.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release trust schema dialect") as captured:
        validate_contract(json.dumps(fixture_selection()).encode("utf-8"), kind="selection", controller_root=tmp_path)
    assert "fictional-private-marker" not in str(captured.value)
    assert captured.value.__cause__ is None
