"""Shared retained approval-input builders; diagnostic fixtures, not approval."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from scripts import (
    release_approval_input,
    release_bundle,
    release_consumer,
    release_frontier_evidence,
    release_manual_evidence,
)
from tests import rehearsal_support, release_bundle_support


def write_input(root: Path) -> None:
    policy_sha256 = hashlib.sha256(
        (Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json").read_bytes()
    ).hexdigest()
    bundle_payload = b"bundle"
    bundle_checksums = f"{hashlib.sha256(bundle_payload).hexdigest()}  placeholder\n".encode()
    members = {
        "private-candidate-report.json": json.dumps(
            {
                "status": "pass",
                "export_manifest": {
                    "source_commit": "a" * 40,
                    "source_tree": "9" * 40,
                    "exported_tree": "b" * 40,
                    "expected_repository": "mpeter/fieldkit-cli",
                    "planned_tag": "v1.0.0",
                    "policy_sha256": policy_sha256,
                },
                "artifact_validation": {
                    "artifacts": [
                        {"name": "fieldkit.whl", "kind": "wheel", "sha256": "c" * 64, "status": "pass"},
                        {"name": "fieldkit.tar.gz", "kind": "sdist", "sha256": "d" * 64, "status": "pass"},
                    ]
                },
            }
        ).encode(),
        "public-candidate/report.json": json.dumps(
            {
                "status": "pass",
                "export_manifest": {
                    "source_commit": "e" * 40,
                    "exported_tree": "b" * 40,
                    "expected_repository": "mpeter/fieldkit-cli",
                    "planned_tag": "v1.0.0",
                    "policy_sha256": policy_sha256,
                },
                "artifact_validation": {
                    "artifacts": [
                        {"name": "fieldkit.whl", "kind": "wheel", "sha256": "c" * 64, "status": "pass"},
                        {"name": "fieldkit.tar.gz", "kind": "sdist", "sha256": "d" * 64, "status": "pass"},
                    ]
                },
            }
        ).encode(),
        "public-candidate/bundle/SHA256SUMS": bundle_checksums,
        "evidence/ledger.json": json.dumps(
            {
                "criteria": [
                    {
                        "id": identifier,
                        "record": {"path": "record.json" if identifier == "cutover-approval" else f"{identifier}.json"},
                    }
                    for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
                ]
            }
        ).encode(),
        "cutover-record.json": b"{}",
        "evidence/record.json": b"{}",
    }
    members.update(
        {
            f"evidence/{identifier}.json": b"{}"
            for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
            if identifier != "cutover-approval"
        }
    )
    for name, data in members.items():
        member_path = root / name
        member_path.parent.mkdir(parents=True, exist_ok=True)
        member_path.write_bytes(data)
    (root / "public-candidate/bundle/placeholder").write_bytes(bundle_payload)
    manifest = {
        "schema_version": 1,
        "status": "pass",
        "candidate": {
            "repository": "mpeter/fieldkit-cli",
            "private_source_sha": "a" * 40,
            "exported_tree": "b" * 40,
            "planned_tag": "v1.0.0",
            "artifacts": [
                {"name": "fieldkit.whl", "kind": "wheel", "sha256": "c" * 64},
                {"name": "fieldkit.tar.gz", "kind": "sdist", "sha256": "d" * 64},
            ],
        },
        "public": {"initial_commit": "e" * 40, "initial_tree": "b" * 40},
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in members.items()],
    }
    (root / "approval-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def expected_input(root: Path) -> release_approval_input.ExpectedApprovalIdentity:
    """Snapshot fixture expectations before deliberately changing the subject."""
    manifest = json.loads((root / "approval-manifest.json").read_bytes())
    private = json.loads((root / "private-candidate-report.json").read_bytes())
    return release_approval_input.ExpectedApprovalIdentity(
        repository=manifest["candidate"]["repository"],
        private_source_sha=manifest["candidate"]["private_source_sha"],
        private_source_tree=private["export_manifest"]["source_tree"],
        exported_tree=manifest["candidate"]["exported_tree"],
        public_commit=manifest["public"]["initial_commit"],
        public_tree=manifest["public"]["initial_tree"],
        planned_tag=manifest["candidate"]["planned_tag"],
        artifacts=tuple(
            sorted((item["name"], item["kind"], item["sha256"]) for item in manifest["candidate"]["artifacts"])
        ),
        documentation_contract_sha256=hashlib.sha256(
            (Path(__file__).parents[1] / "docs/documentation-contract.json").read_bytes()
        ).hexdigest(),
        public_tree_policy_sha256=hashlib.sha256(
            (Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json").read_bytes()
        ).hexdigest(),
    )


def write_selection(root: Path) -> Path:
    selection = root.with_suffix(".selection.json")
    selection.write_text(json.dumps(asdict(expected_input(root))), encoding="utf-8")
    return selection


def write_real_input(root: Path) -> None:
    bundle_candidate = release_bundle_support.candidate(root / "fixture")
    report_path = bundle_candidate / "report.json"
    public = json.loads(report_path.read_text(encoding="utf-8"))
    public["expected_repository"] = "mpeter/fieldkit-cli"
    public["export_manifest"]["expected_repository"] = "mpeter/fieldkit-cli"
    public["scan"]["expected_repository"] = "mpeter/fieldkit-cli"
    public["export_manifest"]["policy_sha256"] = hashlib.sha256(
        (Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json").read_bytes()
    ).hexdigest()
    public["license_evidence"]["export_policy_sha256"] = public["export_manifest"]["policy_sha256"]
    public["scan"]["export_policy_sha256"] = public["export_manifest"]["policy_sha256"]
    report_path.write_text(json.dumps(public), encoding="utf-8")
    release_bundle.materialize(bundle_candidate)
    public_bytes = report_path.read_bytes()

    private = json.loads(json.dumps(public))
    private["export_manifest"]["source_commit"] = "f" * 40
    private["artifact_validation"]["source_revision"] = "f" * 40
    private["license_evidence"]["revision"] = "f" * 40
    private["scan"]["source_commit"] = "f" * 40
    artifacts = [
        {"name": item["name"], "kind": item["kind"], "sha256": item["sha256"]}
        for item in public["artifact_validation"]["artifacts"]
    ]
    cutover = {
        "schema_version": 1,
        "candidate": {
            "repository": "mpeter/fieldkit-cli",
            "source_sha": "f" * 40,
            "source_tree": public["export_manifest"]["source_tree"],
            "export_policy_sha256": public["export_manifest"]["policy_sha256"],
            "exported_tree": public["export_manifest"]["exported_tree"],
            "planned_tag": "v1.0.0",
            "artifacts": artifacts,
        },
        "public": {
            "initial_commit": public["export_manifest"]["source_commit"],
            "initial_tree": public["export_manifest"]["exported_tree"],
            "repository_id": 1,
            "workflow_runs": [
                {
                    "name": "Cutover verification",
                    "path": ".github/workflows/cutover.yml",
                    "event": "push",
                    "id": 4,
                    "attempt": 1,
                    "head_sha": public["export_manifest"]["source_commit"],
                    "conclusion": "success",
                }
            ],
        },
    }
    cutover_bytes = json.dumps(cutover, sort_keys=True).encode()
    evidence_artifacts = [{"name": item["name"], "sha256": item["sha256"]} for item in artifacts]
    evidence_candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": public["export_manifest"]["source_commit"],
        "exported_tree": public["export_manifest"]["exported_tree"],
        "planned_tag": "v1.0.0",
        "artifacts": evidence_artifacts,
    }
    consumer_receipt = {
        "schema_version": 5,
        "status": "success",
        "repository": "mpeter/fieldkit-cli",
        "source_commit": evidence_candidate["source_sha"],
        "planned_tag": "v1.0.0",
        "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "environment": {"system": "Linux", "machine": "x86_64", "python_version": "3.11"},
        "artifacts": [
            {**artifact, "kind": source["kind"], "observed": True, "downloaded": True}
            for artifact, source in zip(evidence_artifacts, artifacts, strict=True)
        ],
        "scenarios": [
            {
                "artifact_name": artifact["name"],
                "id": scenario_id,
                "argv": list(argv),
                "exit_status": 0,
                "status": "pass",
            }
            for artifact in evidence_artifacts
            for scenario_id, argv in release_consumer._OFFLINE_SCENARIOS
        ],
        "findings": [],
    }
    consumer_bytes = json.dumps(consumer_receipt, sort_keys=True).encode()
    contract_bytes = (Path(__file__).parents[1] / "docs/documentation-contract.json").read_bytes()
    rehearsal_receipt, rehearsal_members = rehearsal_support.fixture_receipt("public-release")
    rehearsal_receipt = rehearsal_support.mapping(
        json.loads(json.dumps(rehearsal_receipt).replace('"evidence/', '"streams/'))
    )
    rehearsal_members = {
        name.replace("evidence/", "streams/", 1): raw.replace(b'"evidence/', b'"streams/')
        for name, raw in rehearsal_members.items()
    }
    subject = rehearsal_support.mapping(rehearsal_receipt["subject"])
    subject.update(
        private_source_sha="f" * 40,
        private_source_tree=public["export_manifest"]["source_tree"],
        exported_tree=evidence_candidate["exported_tree"],
        documentation_contract_sha256=hashlib.sha256(contract_bytes).hexdigest(),
    )
    artifact_observations = [
        {**artifact, "size": len((bundle_candidate / "bundle" / artifact["name"]).read_bytes())}
        for artifact in artifacts
    ]
    rehearsal_receipt["artifacts"] = artifact_observations
    private_name = rehearsal_support.mapping(rehearsal_receipt["private_receipt"])["name"]
    assert isinstance(private_name, str)
    linked_private = json.loads(rehearsal_members[private_name])
    linked_private["subject"] = subject
    linked_private["artifacts"] = artifact_observations
    rehearsal_members[private_name] = json.dumps(linked_private).encode()
    rehearsal_receipt["private_receipt"] = rehearsal_support.descriptor(private_name, rehearsal_members[private_name])
    rehearsal_receipt["members"] = [rehearsal_support.descriptor(name, raw) for name, raw in rehearsal_members.items()]
    public_observation = rehearsal_support.mapping(rehearsal_receipt["public"])
    public_observation["commit_sha"] = evidence_candidate["source_sha"]
    public_observation["tree"] = evidence_candidate["exported_tree"]
    run = rehearsal_support.mapping(public_observation["cutover_run"])
    run["id"] = 3
    run["head_sha"] = evidence_candidate["source_sha"]
    scenarios = rehearsal_receipt["scenarios"]
    assert isinstance(scenarios, list)
    rehearsal_ids: list[str] = []
    for item in scenarios:
        identifier = rehearsal_support.mapping(item)["id"]
        assert isinstance(identifier, str)
        rehearsal_ids.append(identifier)
    rehearsal_bytes = json.dumps(rehearsal_receipt, sort_keys=True).encode()
    prompt = (
        f"Review source {evidence_candidate['source_sha']} as the final release candidate in fresh-eyes mode. "
        "Cover architecture, Python, CLI adapters, configuration, error/exit handling, persistence, integrations, "
        "tests, CI, documentation, agent instructions, and references."
    ).encode()
    structured_review = {
        "schema_version": 1,
        "review_mode": "fresh-eyes",
        "scope": "final-release-candidate",
        "reviewed_revision": evidence_candidate["source_sha"],
        "decision": "pass",
        "findings": [],
        "unresolved_findings": [],
    }
    response = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "No unresolved findings.",
        "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
        "structured_output": structured_review,
    }
    response_bytes = json.dumps(response, sort_keys=True).encode()
    version_bytes = b"2.1.283 (Claude Code)\n"
    support_members = {
        "evidence/support/consumer.json": consumer_bytes,
        "evidence/support/rehearsal.json": rehearsal_bytes,
        "evidence/support/frontier-prompt.txt": prompt,
        "evidence/support/frontier-response.json": response_bytes,
        "evidence/support/frontier-version.txt": version_bytes,
        **{f"evidence/{name}": raw for name, raw in rehearsal_members.items()},
    }
    payloads = {
        "github-release-environment": {
            "repository_id": 1,
            "environment": "release-approval",
            "required_reviewers": ["mpeter"],
            "workflow_path": ".github/workflows/release-approval.yml",
        },
        "pypi-trusted-publisher": {
            "project": "fieldkit-cli",
            "repository": "mpeter/fieldkit-cli",
            "workflow_path": ".github/workflows/release.yml",
            "environment": "pypi",
        },
        "package-name-reservation": {
            "project": "fieldkit-cli",
            "index_endpoint": "https://pypi.org/pypi/fieldkit-cli/json",
            "availability": "reserved",
        },
        "repository-controls": {
            "repository_id": 1,
            "default_branch": "main",
            "required_checks": ["Required checks", "Changelog fragment"],
            "fork_ci": True,
        },
        "testpypi-rehearsal": {
            "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
            "consumer_evidence_path": "support/consumer.json",
            "consumer_evidence_sha256": hashlib.sha256(consumer_bytes).hexdigest(),
            "artifacts": evidence_artifacts,
        },
        "public-contributor-journeys": {
            "workflow_run_id": 3,
            "journeys": sorted(release_manual_evidence.journey_ids("external_contributor")),
            "artifacts": evidence_artifacts,
            "rehearsal_evidence_path": "support/rehearsal.json",
            "rehearsal_evidence_sha256": hashlib.sha256(rehearsal_bytes).hexdigest(),
        },
        "public-user-journeys": {
            "workflow_run_id": 3,
            "journeys": sorted(release_manual_evidence.journey_ids("external_user")),
            "artifacts": evidence_artifacts,
            "rehearsal_evidence_path": "support/rehearsal.json",
            "rehearsal_evidence_sha256": hashlib.sha256(rehearsal_bytes).hexdigest(),
        },
        "documentation-rehearsals": {
            "workflow_run_id": 3,
            "rehearsal_evidence_path": "support/rehearsal.json",
            "rehearsal_evidence_sha256": hashlib.sha256(rehearsal_bytes).hexdigest(),
            "verified_blocks": [],
            "scenarios": sorted(rehearsal_ids),
        },
        "frontier-final-review": {
            "reviewed_revision": public["export_manifest"]["source_commit"],
            "reviewer_role": "independent-model",
            "model": "claude-opus-5-5",
            "review_mode": "fresh-eyes",
            "scope": "final-release-candidate",
            "cli_version": "2.1.283 (Claude Code)",
            "cli_version_path": "support/frontier-version.txt",
            "cli_version_sha256": hashlib.sha256(version_bytes).hexdigest(),
            "cli_argv": [*release_frontier_evidence.CLI_ARGV, prompt.decode()],
            "prompt_path": "support/frontier-prompt.txt",
            "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
            "response_path": "support/frontier-response.json",
            "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "findings": [],
            "unresolved_findings": [],
        },
        "cutover-approval": {
            "cutover_record_sha256": hashlib.sha256(cutover_bytes).hexdigest(),
            "initial_commit": public["export_manifest"]["source_commit"],
            "initial_tree": public["export_manifest"]["exported_tree"],
            "workflow_runs": [4],
        },
    }
    sources = {
        "github-release-environment": "https://api.github.com/repos/mpeter/fieldkit-cli/environments/release-approval",
        "pypi-trusted-publisher": "https://github.com/mpeter/fieldkit-cli/actions/workflows/release.yml",
        "package-name-reservation": "https://pypi.org/pypi/fieldkit-cli/json",
        "repository-controls": "https://api.github.com/repos/mpeter/fieldkit-cli",
        "testpypi-rehearsal": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "public-contributor-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/3",
        "public-user-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/3",
        "documentation-rehearsals": "https://github.com/mpeter/fieldkit-cli/actions/runs/3",
        "frontier-final-review": "https://github.com/mpeter/fieldkit-cli/pull/1",
        "cutover-approval": "https://github.com/mpeter/fieldkit-cli/actions/runs/4",
    }
    criteria = []
    evidence_members = dict(support_members)
    for gate in release_manual_evidence.REQUIRED_EVIDENCE_IDS:
        payload = payloads[gate]
        record = {
            "schema_version": 1,
            "kind": gate,
            "status": "pass",
            "candidate": evidence_candidate,
            "proof": {
                "source_uri": sources[gate],
                "captured_at": datetime.now(UTC).isoformat(),
                "payload": payload,
                "payload_sha256": release_manual_evidence.canonical_json_sha256(payload),
            },
        }
        record_bytes = json.dumps(record, sort_keys=True).encode()
        path = f"records/{gate}.json"
        evidence_members[f"evidence/{path}"] = record_bytes
        criteria.append(
            {
                "id": gate,
                "status": "pass",
                "record": {
                    "kind": gate,
                    "path": path,
                    "uri": sources[gate],
                    "sha256": hashlib.sha256(record_bytes).hexdigest(),
                },
            }
        )
    ledger_bytes = json.dumps(
        {"schema_version": 1, "candidate": evidence_candidate, "criteria": criteria}, sort_keys=True
    ).encode()
    members = {
        "private-candidate-report.json": json.dumps(private, sort_keys=True).encode(),
        "public-candidate/report.json": public_bytes,
        "public-candidate/bundle/SHA256SUMS": (bundle_candidate / "bundle" / "SHA256SUMS").read_bytes(),
        "evidence/ledger.json": ledger_bytes,
        "cutover-record.json": cutover_bytes,
        **evidence_members,
    }
    for name, data in members.items():
        member_path = root / name
        member_path.parent.mkdir(parents=True, exist_ok=True)
        member_path.write_bytes(data)
    shutil.copytree(bundle_candidate / "bundle", root / "public-candidate" / "bundle", dirs_exist_ok=True)
    manifest = {
        "schema_version": 1,
        "status": "pass",
        "candidate": {
            "repository": "mpeter/fieldkit-cli",
            "private_source_sha": "f" * 40,
            "exported_tree": public["export_manifest"]["exported_tree"],
            "planned_tag": "v1.0.0",
            "artifacts": artifacts,
        },
        "public": {
            "initial_commit": public["export_manifest"]["source_commit"],
            "initial_tree": public["export_manifest"]["exported_tree"],
        },
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in members.items()],
    }
    (root / "approval-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.rmtree(root / "fixture")
