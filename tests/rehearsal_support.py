"""Shared diagnostic rehearsal receipts; matching identities grant no approval."""

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from scripts import rehearsal_evidence as evidence
from scripts.documentation_commands import OUTER_SCENARIOS, OuterScenario
from scripts.documentation_manual import MANUAL_SCENARIOS, ManualScenario


def schema_bytes() -> bytes:
    return (Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json").read_bytes()


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def items(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def descriptor(name: str, data: bytes) -> dict[str, object]:
    return {"name": name, "sha256": sha256(data).hexdigest(), "size": len(data)}


def fixture_receipt(phase: str = "private-candidate") -> tuple[dict[str, object], dict[str, bytes]]:
    members = {"evidence/empty.txt": b""}
    stream = {
        "full_sha256": sha256(b"").hexdigest(),
        "total_bytes": 0,
        "capture_limit_bytes": 4096,
        "truncated": False,
        "redacted": False,
        "retained": descriptor("evidence/empty.txt", b""),
    }
    manual = [
        scenario
        for scenario in MANUAL_SCENARIOS
        if (scenario.proof_type == "credentialed-integration") == (phase == "private-candidate")
    ]
    scenarios: list[dict[str, object]] = []
    registrations: tuple[ManualScenario | OuterScenario, ...] = (
        *manual,
        *(OUTER_SCENARIOS if phase == "public-release" else ()),
    )
    for registration in registrations:
        command: dict[str, object] = {
            "started_at": "2026-09-29T11:00:00Z",
            "finished_at": "2026-09-29T11:00:01Z",
            "resources": {
                "processes": 16,
                "memory_bytes": 1048576,
                "cpu_seconds": 60,
                "scratch_bytes": 4096,
                "output_bytes": 4096,
                "wall_seconds": 60,
            },
            "argv": ["fieldkit", "--help"] if isinstance(registration, ManualScenario) else list(registration.argv),
            "process": {"exit_code": 0, "signal": None},
            "timeout": {"triggered": False, "deadline_seconds": 60},
            "policy": {
                "verdict": "allow",
                "complete": True,
                "lost_events": 0,
                "observation": descriptor("evidence/empty.txt", b""),
            },
            "teardown": {"result": "verified-empty", "observation": descriptor("evidence/empty.txt", b"")},
            "stdout": dict(stream),
            "stderr": dict(stream),
            "assertions": []
            if isinstance(registration, ManualScenario)
            else [asdict(assertion) for assertion in registration.assertions],
            "behavioral_assertions": [],
        }
        scenarios.append(
            {
                "id": registration.scenario_id if isinstance(registration, ManualScenario) else registration.identifier,
                "precondition_id": registration.precondition_id,
                "documented_block_sha256": registration.sha256 if isinstance(registration, ManualScenario) else None,
                "actor": registration.actor,
                "commands": [command],
            }
        )
    receipt: dict[str, object] = {
        "schema_version": 2,
        "phase": phase,
        "subject": {
            "source_kind": "initial-export",
            "private_source_sha": "a" * 40,
            "private_source_tree": "b" * 40,
            "exported_tree": "c" * 40,
            "documentation_contract_sha256": "d" * 64,
        },
        "control": {
            "controller_revision": "e" * 40,
            "controller_set_sha256": "f" * 64,
            "registry_sha256": "1" * 64,
            "schema_sha256": sha256(schema_bytes()).hexdigest(),
            "toolchain_sha256": "2" * 64,
            "policy_sha256": "3" * 64,
            "selection_record_sha256": "4" * 64,
        },
        "environment": {
            "os": "fixture-os",
            "architecture": "fixture-arch",
            "python": "3.11",
            "uv": "fixture",
            "git": "fixture",
            "make": "fixture",
        },
        "artifacts": [
            {
                "name": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "kind": "wheel",
                "size": 1,
                "sha256": sha256(b"w").hexdigest(),
            },
            {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "size": 1, "sha256": sha256(b"s").hexdigest()},
        ],
        "scenarios": scenarios,
        "review": {
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-09-29T12:00:00Z",
            "immutable_url": "https://example.com/reviews/fixture",
        },
        "members": [descriptor(name, data) for name, data in members.items()],
    }
    if phase == "public-release":
        private, _ = fixture_receipt()
        private_bytes = json.dumps(private).encode()
        members["evidence/private-rehearsal.json"] = private_bytes
        receipt["members"] = [descriptor(name, data) for name, data in members.items()]
        receipt["private_receipt"] = descriptor("evidence/private-rehearsal.json", private_bytes)
        receipt["public"] = {
            "repository": "mpeter/fieldkit-cli",
            "repository_id": 1,
            "commit_sha": "7" * 40,
            "tree": "c" * 40,
            "cutover_run": {
                "id": 1,
                "attempt": 1,
                "workflow_id": 1,
                "name": "Cutover verification",
                "path": ".github/workflows/cutover.yml",
                "event": "push",
                "head_sha": "7" * 40,
            },
        }
    return receipt, members


def validate(
    receipt: dict[str, object],
    members: dict[str, bytes],
    *,
    context: evidence.ProtectedRehearsalContext | None = None,
    schema: bytes | None = None,
) -> evidence.RehearsalValidation:
    return evidence.validate_rehearsal(
        json.dumps(receipt).encode(),
        schema_bytes=schema or schema_bytes(),
        context=context,
        retained_members=members,
        artifact_bytes={
            str(mapping(item)["name"]): b"w" if mapping(item)["kind"] == "wheel" else b"s"
            for item in items(receipt["artifacts"])
        },
    )
