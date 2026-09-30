"""Contracts for the release-readiness evidence entry point."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import (
    _release_governance,
    check_public_candidate,
    git_worktree,
    json_policy,
    rehearsal_acquisition,
    release_check,
    release_frontier_evidence,
    release_manual_evidence,
)
from scripts.documentation_commands import OUTER_SCENARIOS
from tests import release_approval_support

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "key", ["synthetic-private-key\n\x1b[31m", "synthetic-private-key" + "x" * 8192], ids=["control", "long"]
)
def test_duplicate_json_hook_has_fixed_diagnostic(key: str) -> None:
    with pytest.raises(ValueError) as caught:
        json_policy.reject_duplicate_json_keys([(key, 1), (key, 2)])
    assert str(caught.value) == "duplicate JSON key"


@pytest.mark.parametrize(
    "key", ["synthetic-private-key\n\x1b[31m", "synthetic-private-key" + "x" * 8192], ids=["control", "long"]
)
def test_release_record_json_diagnostic_never_reflects_decoded_key(key: str, tmp_path: Path) -> None:
    encoded = json.dumps(key)
    payload = ("{" + encoded + ":1," + encoded + ":2}").encode()
    path = tmp_path / "record.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError) as caught:
        release_manual_evidence._manual_record_bytes(path, sha256(payload).hexdigest())
    assert str(caught.value) == "manual release evidence record is not valid JSON"


def test_canonical_json_container_depth_boundary() -> None:
    at_limit = ("[" * 64 + "0" + "]" * 64).encode()
    parsed = json_policy.load_json_bytes(at_limit)
    assert isinstance(parsed, list)
    with pytest.raises(ValueError, match=r"^JSON input exceeds nesting limit$"):
        json_policy.load_json_bytes(("[" * 65 + "0" + "]" * 65).encode())


def test_canonical_depth_check_rejects_decoded_cycle() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match=r"^JSON input exceeds nesting limit$"):
        json_policy.require_bounded_json_depth(cyclic)


def _captured_now() -> str:
    return datetime.now(UTC).isoformat()


def _candidate() -> SimpleNamespace:
    return SimpleNamespace()


def _policy() -> SimpleNamespace:
    return SimpleNamespace(candidate=SimpleNamespace(repository="example/fieldkit-cli", planned_tag="v1.0.0"))


def test_pending_external_controls_write_json_and_return_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    revision = "a" * 40
    monkeypatch.setattr(git_worktree, "require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(git_worktree, "head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    monkeypatch.setattr(release_check, "_governance", lambda *_args: ("pending", ("pypi-trusted-publisher",)))
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                revision,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
                "--json",
            ]
        )
        == 1
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pending"
    assert report["criteria"][-1]["id"] == "cutover-approval"
    assert {criterion["id"] for criterion in report["criteria"]} >= {
        "documentation-rehearsals",
        "package-name-reservation",
        "public-contributor-journeys",
        "public-user-journeys",
        "repository-controls",
        "testpypi-rehearsal",
    }


def test_pending_manual_gates_prevent_pass_when_governance_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(git_worktree, "require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(git_worktree, "head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    monkeypatch.setattr(release_check, "_governance", lambda *_args: ("pass", ()))
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                revision,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 1
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pending"
    assert {criterion["status"] for criterion in report["criteria"]} == {"pass", "pending"}


def test_external_ledger_closes_policy_controls_without_editing_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(git_worktree, "require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(git_worktree, "head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    monkeypatch.setattr(
        release_check,
        "_governance",
        lambda *_args: ("pending", ("github-release-environment", "pypi-trusted-publisher")),
    )
    monkeypatch.setattr(
        release_manual_evidence,
        "validate_paths",
        lambda *_args, **_kwargs: (
            tuple(
                release_manual_evidence.Criterion(identifier, "pass", "https://example.com/evidence", "e" * 64)
                for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
            ),
            "f" * 64,
        ),
    )
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                revision,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
                "--manual-evidence",
                str(tmp_path / "release-evidence.json"),
            ]
        )
        == 0
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pass"
    assert {criterion["id"] for criterion in report["criteria"]} == {
        "source-revision",
        "public-candidate",
        *release_manual_evidence.REQUIRED_EVIDENCE_IDS,
    }


def test_external_ledger_cannot_hide_an_unknown_pending_governance_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(git_worktree, "require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(git_worktree, "head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    monkeypatch.setattr(release_check, "_governance", lambda *_args: ("pending", ("future-live-control",)))
    monkeypatch.setattr(
        release_manual_evidence,
        "validate_paths",
        lambda *_args, **_kwargs: (
            tuple(
                release_manual_evidence.Criterion(identifier, "pass", "https://example.com/evidence", "e" * 64)
                for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
            ),
            "f" * 64,
        ),
    )
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                revision,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
                "--manual-evidence",
                str(tmp_path / "release-evidence.json"),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "pending"
    assert {item["id"] for item in report["criteria"] if item["status"] == "pending"} == {"future-live-control"}


def test_manual_bytes_api_rejects_invalid_ledger_without_filesystem_acquisition() -> None:
    with pytest.raises(ValueError, match="ledger"):
        release_manual_evidence.validate_bytes(
            Path(__file__).parents[1],
            documentation_contract_sha256="a" * 64,
            evidence_bytes=b"not-json",
            candidate_report_bytes=b"{}",
            record_bytes={},
        )


def test_legacy_true_receipt_mock_cannot_close_rehearsal_criteria(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate_report = {
        "expected_repository": "mpeter/fieldkit-cli",
        "package": "fieldkit-cli",
        "planned_tag": "v1.0.0",
        "status": "pass",
        "export_manifest": {
            "expected_repository": "mpeter/fieldkit-cli",
            "source_commit": "a" * 40,
            "exported_tree": "b" * 40,
            "planned_tag": "v1.0.0",
        },
        "artifact_validation": {
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "c" * 64, "status": "pass"},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "d" * 64, "status": "pass"},
            ]
        },
    }
    candidate_path = tmp_path / "candidate-report.json"
    candidate_path.write_text(json.dumps(candidate_report), encoding="utf-8")
    evidence_candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [
            {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "c" * 64},
            {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "d" * 64},
        ],
    }
    consumer_receipt = b'{"retained":"consumer"}'
    rehearsal_receipt = b'{"retained":"rehearsal"}'
    frontier_prompt = (
        f"Fresh-eyes review of final release candidate {evidence_candidate['source_sha']}: "
        + ", ".join(release_frontier_evidence.PROMPT_TERMS)
    ).encode()
    frontier_response = b'{"retained":"frontier-response"}'
    frontier_version = b"2.1.283 (Claude Code)\n"
    support = {
        "consumer-evidence.json": consumer_receipt,
        "rehearsal-evidence.json": rehearsal_receipt,
        "frontier-prompt.txt": frontier_prompt,
        "frontier-response.json": frontier_response,
        "frontier-version.txt": frontier_version,
    }
    for path, raw in support.items():
        (tmp_path / path).write_bytes(raw)
    monkeypatch.setattr(release_manual_evidence, "_consumer_receipt_valid", lambda *_args: True)
    monkeypatch.setattr(release_manual_evidence, "_rehearsal_receipt_valid", lambda *_args: True, raising=False)
    monkeypatch.setattr(release_frontier_evidence, "valid_review", lambda *_args: True)
    criteria = []
    required_ids = (
        "github-release-environment",
        "pypi-trusted-publisher",
        *release_manual_evidence.MANUAL_GATES,
    )
    proof_payloads = {
        "github-release-environment": {
            "repository_id": 1,
            "environment": "release-approval",
            "required_reviewers": [
                _release_governance.load_policy(
                    Path(__file__).parents[1] / release_manual_evidence.GOVERNANCE_POLICY
                ).roles["approver_login"]
            ],
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
            "consumer_evidence_path": "consumer-evidence.json",
            "consumer_evidence_sha256": sha256(consumer_receipt).hexdigest(),
            "artifacts": evidence_candidate["artifacts"],
        },
        "public-contributor-journeys": {
            "workflow_run_id": 1,
            "journeys": sorted(release_manual_evidence.journey_ids("external_contributor")),
            "artifacts": evidence_candidate["artifacts"],
            "rehearsal_evidence_path": "rehearsal-evidence.json",
            "rehearsal_evidence_sha256": sha256(rehearsal_receipt).hexdigest(),
        },
        "public-user-journeys": {
            "workflow_run_id": 2,
            "journeys": sorted(release_manual_evidence.journey_ids("external_user")),
            "artifacts": evidence_candidate["artifacts"],
            "rehearsal_evidence_path": "rehearsal-evidence.json",
            "rehearsal_evidence_sha256": sha256(rehearsal_receipt).hexdigest(),
        },
        "documentation-rehearsals": {
            "workflow_run_id": 3,
            "rehearsal_evidence_path": "rehearsal-evidence.json",
            "rehearsal_evidence_sha256": sha256(rehearsal_receipt).hexdigest(),
            "verified_blocks": ["readme.md.block-1"],
            "scenarios": sorted(scenario.identifier for scenario in OUTER_SCENARIOS),
        },
        "frontier-final-review": {
            "reviewed_revision": "a" * 40,
            "reviewer_role": "independent-model",
            "model": "claude-opus-5-5",
            "review_mode": "fresh-eyes",
            "scope": "final-release-candidate",
            "cli_version": "2.1.283 (Claude Code)",
            "cli_version_path": "frontier-version.txt",
            "cli_version_sha256": sha256(frontier_version).hexdigest(),
            "cli_argv": [*release_frontier_evidence.CLI_ARGV, frontier_prompt.decode()],
            "prompt_path": "frontier-prompt.txt",
            "prompt_sha256": sha256(frontier_prompt).hexdigest(),
            "response_path": "frontier-response.json",
            "response_sha256": sha256(frontier_response).hexdigest(),
            "findings": [],
            "unresolved_findings": [],
        },
        "cutover-approval": {
            "cutover_record_sha256": "a" * 64,
            "initial_commit": "a" * 40,
            "initial_tree": "b" * 40,
            "workflow_runs": [4],
        },
    }
    proof_sources = {
        "github-release-environment": "https://api.github.com/repos/mpeter/fieldkit-cli/environments/release-approval",
        "pypi-trusted-publisher": "https://github.com/mpeter/fieldkit-cli/actions/workflows/release.yml",
        "package-name-reservation": "https://pypi.org/pypi/fieldkit-cli/json",
        "repository-controls": "https://api.github.com/repos/mpeter/fieldkit-cli",
        "testpypi-rehearsal": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "public-contributor-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/1",
        "public-user-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/2",
        "documentation-rehearsals": "https://github.com/mpeter/fieldkit-cli/actions/runs/3",
        "frontier-final-review": "https://github.com/mpeter/fieldkit-cli/pull/1",
        "cutover-approval": "https://github.com/mpeter/fieldkit-cli/actions/runs/4",
    }
    for gate in required_ids:
        path = f"{gate}.json"
        payload = proof_payloads[gate]
        record_bytes = json.dumps(
            {
                "schema_version": 1,
                "kind": gate,
                "status": "pass",
                "candidate": evidence_candidate,
                "proof": {
                    "source_uri": proof_sources[gate],
                    "captured_at": _captured_now(),
                    "payload": payload,
                    "payload_sha256": release_manual_evidence.canonical_json_sha256(payload),
                },
            },
            sort_keys=True,
        ).encode()
        (tmp_path / path).write_bytes(record_bytes)
        criteria.append(
            {
                "id": gate,
                "status": "pass",
                "record": {
                    "kind": gate,
                    "path": path,
                    "uri": proof_sources[gate],
                    "sha256": sha256(record_bytes).hexdigest(),
                },
            }
        )
    evidence_path = tmp_path / "manual-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": evidence_candidate,
                "criteria": criteria,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rehearsal"):
        release_manual_evidence.validate_paths(Path(__file__).parents[1], evidence_path, candidate_path)


def test_manual_evidence_rejects_a_record_with_a_claimed_digest(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"
    record_path.write_text('{"schema_version": 1, "kind": "x", "status": "pass", "candidate": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="record digest"):
        release_manual_evidence._manual_record_bytes(record_path, "0" * 64)


def test_manual_evidence_rejects_a_minimal_self_authored_claim() -> None:
    """A passing status and candidate copy are not a criterion-specific proof."""
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }

    with pytest.raises(ValueError, match="no typed proof"):
        release_manual_evidence._validate_manual_record(
            Path(__file__).parents[1],
            "github-release-environment",
            {
                "schema_version": 1,
                "kind": "github-release-environment",
                "status": "pass",
                "candidate": candidate,
            },
            candidate,
            {},
            set(),
        )


def test_frontier_final_review_rejects_an_early_unstructured_cli_response() -> None:
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [
            {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "c" * 64},
            {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "d" * 64},
        ],
    }
    prompt = (
        f"Fresh-eyes review of final release candidate {candidate['source_sha']}: "
        + ", ".join(release_frontier_evidence.PROMPT_TERMS)
    ).encode()
    response = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "This is an early scoped review, not a release approval.",
            "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5", "provider": "firstParty"}},
            "structured_output": None,
        }
    ).encode()
    payload: dict[str, object] = {
        "reviewed_revision": "a" * 40,
        "reviewer_role": "independent-model",
        "model": "claude-opus-5-5",
        "review_mode": "fresh-eyes",
        "scope": "final-release-candidate",
        "cli_version": "2.1.283 (Claude Code)",
        "cli_version_path": "frontier-version.txt",
        "cli_version_sha256": sha256(b"2.1.283 (Claude Code)\n").hexdigest(),
        "cli_argv": [*release_frontier_evidence.CLI_ARGV, prompt.decode()],
        "prompt_path": "frontier-prompt.txt",
        "prompt_sha256": sha256(prompt).hexdigest(),
        "response_path": "frontier-response.json",
        "response_sha256": sha256(response).hexdigest(),
        "findings": [],
        "unresolved_findings": [],
    }
    record = {
        "schema_version": 1,
        "kind": "frontier-final-review",
        "status": "pass",
        "candidate": candidate,
        "proof": {
            "source_uri": "https://github.com/mpeter/fieldkit-cli/pull/1",
            "captured_at": _captured_now(),
            "payload": payload,
            "payload_sha256": release_manual_evidence.canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match="invalid typed proof values"):
        release_manual_evidence._validate_manual_record(
            Path(__file__).parents[1],
            "frontier-final-review",
            record,
            candidate,
            {
                "frontier-prompt.txt": prompt,
                "frontier-response.json": response,
                "frontier-version.txt": b"2.1.283 (Claude Code)\n",
            },
            set(),
        )


def test_frontier_final_review_rejects_an_invented_model_name() -> None:
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    prompt = (
        f"Fresh-eyes review of final release candidate {candidate['source_sha']}: "
        + ", ".join(release_frontier_evidence.PROMPT_TERMS)
    ).encode()
    structured = {
        "schema_version": 1,
        "review_mode": "fresh-eyes",
        "scope": "final-release-candidate",
        "reviewed_revision": candidate["source_sha"],
        "decision": "pass",
        "findings": [],
        "unresolved_findings": [],
    }
    response = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "pass",
            "modelUsage": {"Claude Frontier": {"canonicalModel": "Claude Frontier"}},
            "structured_output": structured,
        }
    ).encode()
    payload = {
        "reviewed_revision": candidate["source_sha"],
        "model": "Claude Frontier",
        "findings": [],
        "unresolved_findings": [],
    }

    assert not release_frontier_evidence.valid_review(prompt, response, payload, candidate)


def test_frontier_payload_binds_the_retained_prompt_and_version_to_exact_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = b"review exact candidate " + b"a" * 40
    version = b"2.1.283 (Claude Code)\n"
    response = b"{}"
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = {
        "reviewed_revision": "a" * 40,
        "reviewer_role": "independent-model",
        "model": "claude-opus-5-5",
        "review_mode": "fresh-eyes",
        "scope": "final-release-candidate",
        "cli_version": "2.1.283 (Claude Code)",
        "cli_version_path": "version.txt",
        "cli_version_sha256": sha256(version).hexdigest(),
        "cli_argv": list(release_frontier_evidence.CLI_ARGV),
        "prompt_path": "prompt.txt",
        "prompt_sha256": sha256(prompt).hexdigest(),
        "response_path": "response.json",
        "response_sha256": sha256(response).hexdigest(),
        "findings": [],
        "unresolved_findings": [],
    }
    monkeypatch.setattr(release_frontier_evidence, "valid_review", lambda *_args: True)

    assert not release_manual_evidence._valid_payload(
        Path(__file__).parents[1],
        "frontier-final-review",
        payload,
        candidate,
        {"version.txt": version, "prompt.txt": prompt, "response.json": response},
        set(),
    )

    payload["cli_argv"] = [*release_frontier_evidence.CLI_ARGV, prompt.decode()]
    assert release_manual_evidence._valid_payload(
        Path(__file__).parents[1],
        "frontier-final-review",
        payload,
        candidate,
        {"version.txt": version, "prompt.txt": prompt, "response.json": response},
        set(),
    )


def test_release_rehearsal_uses_real_canonical_diagnostics_and_keeps_three_gates_pending(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)
    evidence_path = tmp_path / "evidence/ledger.json"
    result = release_manual_evidence.validate_paths(
        Path(__file__).parents[1],
        evidence_path,
        tmp_path / "public-candidate/report.json",
        candidate_bundle=tmp_path / "public-candidate/bundle",
        private_candidate_report=tmp_path / "private-candidate-report.json",
    )

    criteria, ledger_digest = result
    assert {item.id for item in criteria if item.status == "pending"} == {
        "public-contributor-journeys",
        "public-user-journeys",
        "documentation-rehearsals",
    }
    assert len(criteria) == len(release_manual_evidence.REQUIRED_EVIDENCE_IDS)
    assert ledger_digest == sha256(evidence_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("gate", release_manual_evidence.REQUIRED_EVIDENCE_IDS)
def test_manual_evidence_rejects_null_typed_payload_values(gate: str) -> None:
    """Every external gate needs observations, not a shape-preserving null claim."""
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = dict.fromkeys(release_manual_evidence.RECORD_PAYLOAD_FIELDS[gate])
    record = {
        "schema_version": 1,
        "kind": gate,
        "status": "pass",
        "candidate": candidate,
        "proof": {
            "source_uri": "https://example.com/evidence",
            "captured_at": _captured_now(),
            "payload": payload,
            "payload_sha256": release_manual_evidence.canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match=r"untrusted proof source|invalid typed proof values"):
        release_manual_evidence._validate_manual_record(Path(__file__).parents[1], gate, record, candidate, {}, set())


def test_manual_evidence_rejects_non_iso_capture_time() -> None:
    """A free-form capture time cannot be used to represent fresh manual evidence."""
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = {
        "repository_id": 1,
        "environment": "release-approval",
        "required_reviewers": [],
        "workflow_path": ".github/workflows/release.yml",
    }
    record = {
        "schema_version": 1,
        "kind": "github-release-environment",
        "status": "pass",
        "candidate": candidate,
        "proof": {
            "source_uri": "https://api.github.com/repos/mpeter/fieldkit-cli/environments/release-approval",
            "captured_at": "not-a-date",
            "payload": payload,
            "payload_sha256": release_manual_evidence.canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match="invalid capture time"):
        release_manual_evidence._validate_manual_record(
            Path(__file__).parents[1], "github-release-environment", record, candidate, {}, set()
        )


def test_manual_evidence_rejects_future_capture_time() -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    assert not release_manual_evidence._valid_captured_at(future)


def test_mutable_observation_rejects_a_stale_capture_time() -> None:
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    assert not release_manual_evidence._valid_captured_at(
        stale, maximum_age=release_manual_evidence.MUTABLE_OBSERVATION_MAX_AGE
    )


def test_environment_reviewers_must_match_the_governed_approver_login() -> None:
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = {
        "repository_id": 1,
        "environment": "release-approval",
        "required_reviewers": ["someone-else"],
        "workflow_path": ".github/workflows/release-approval.yml",
    }

    assert not release_manual_evidence._valid_payload(
        Path(__file__).parents[1], "github-release-environment", payload, candidate, {}, set()
    )


def test_manual_evidence_root_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory path"):
        rehearsal_acquisition.open_real_directory(linked)


@pytest.mark.parametrize(
    "source_uri",
    [
        "https://github.com/attacker/fieldkit-cli/actions/workflows/release.yml",
        "https://user@github.com/mpeter/fieldkit-cli/actions/workflows/release.yml",
        "https://github.com/mpeter/fieldkit-cli/actions/workflows/release.yml?ref=other",
    ],
)
def test_manual_evidence_rejects_unscoped_or_ambiguous_source_uri(source_uri: str) -> None:
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = {
        "project": "fieldkit-cli",
        "repository": "mpeter/fieldkit-cli",
        "workflow_path": ".github/workflows/release.yml",
        "environment": "pypi",
    }

    assert not release_manual_evidence._valid_source_uri("pypi-trusted-publisher", source_uri, payload, candidate)


def test_manual_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    evidence_path = tmp_path / "manual-evidence.json"
    evidence_path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match=r"^release evidence JSON is not valid JSON$"):
        release_manual_evidence._load_json(evidence_path)


def test_manual_record_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"
    record_bytes = b'{"schema_version": 1, "schema_version": 1}'
    record_path.write_bytes(record_bytes)

    with pytest.raises(ValueError, match=r"^manual release evidence record is not valid JSON$"):
        release_manual_evidence._manual_record_bytes(record_path, sha256(record_bytes).hexdigest())


def test_stale_revision_writes_failure_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(git_worktree, "require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(git_worktree, "head_revision", lambda _repo: "b" * 40)
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                "a" * 40,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["criteria"][0]["id"] == "source-revision"


def test_dirty_worktree_writes_failure_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        git_worktree,
        "require_clean_worktree",
        lambda _repo: (_ for _ in ()).throw(ValueError("clean worktree")),
    )
    output = tmp_path / "release-check.json"

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                "a" * 40,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    assert "clean worktree" in json.loads(output.read_text(encoding="utf-8"))["criteria"][0]["evidence"]


def test_unexpected_failure_atomically_replaces_a_stale_pass_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "release-check.json"
    output.write_text('{"status":"pass"}\n', encoding="utf-8")
    monkeypatch.setattr(
        git_worktree,
        "require_clean_worktree",
        lambda _repo: (_ for _ in ()).throw(RuntimeError("unexpected verifier failure")),
    )

    assert (
        release_check.main(
            [
                "--repo",
                str(tmp_path),
                "--revision",
                "a" * 40,
                "--output-dir",
                str(tmp_path / "candidate"),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "unexpected verifier failure" in report["criteria"][0]["evidence"]


def test_direct_script_invocation_resolves_its_sibling_modules(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "release_check.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(Path(__file__).parents[1]),
            "--revision",
            "0" * 40,
            "--output-dir",
            str(tmp_path / "candidate"),
            "--output",
            str(tmp_path / "result.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Release check: FAILED" in result.stdout
