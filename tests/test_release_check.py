"""Contracts for the release-readiness evidence entry point."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_check

pytestmark = pytest.mark.unit


def _candidate() -> SimpleNamespace:
    return SimpleNamespace()


def _policy() -> SimpleNamespace:
    return SimpleNamespace(candidate=SimpleNamespace(repository="example/fieldkit-cli", planned_tag="v1.0.0"))


def test_pending_external_controls_write_json_and_return_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    revision = "a" * 40
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(release_check.check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
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
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(release_check.check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
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
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: revision)
    monkeypatch.setattr(release_check, "load_policy", lambda _path: _policy())
    monkeypatch.setattr(release_check.check_public_candidate, "check_candidate", lambda *_args, **_kwargs: _candidate())
    monkeypatch.setattr(
        release_check,
        "_governance",
        lambda *_args: ("pending", ("github-release-environment", "pypi-trusted-publisher")),
    )
    monkeypatch.setattr(
        release_check,
        "_manual_criteria",
        lambda *_args: (
            tuple(
                release_check.Criterion(identifier, "pass", "https://example.com/evidence", "e" * 64)
                for identifier in release_check._REQUIRED_EVIDENCE_IDS
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
        *release_check._REQUIRED_EVIDENCE_IDS,
    }


def test_same_candidate_manual_evidence_closes_every_external_criterion(tmp_path: Path) -> None:
    candidate_report = {
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
    criteria = []
    required_ids = (
        "github-release-environment",
        "pypi-trusted-publisher",
        *release_check._MANUAL_GATES,
    )
    proof_payloads = {
        "github-release-environment": {
            "repository_id": 1,
            "environment": "pypi",
            "required_reviewers": ["maintainer"],
            "workflow_path": ".github/workflows/release.yml",
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
            "availability": "available",
        },
        "repository-controls": {
            "repository_id": 1,
            "default_branch": "main",
            "required_checks": ["CI"],
            "fork_ci": True,
        },
        "testpypi-rehearsal": {
            "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/json",
            "consumer_evidence_sha256": "e" * 64,
            "artifacts": evidence_candidate["artifacts"],
        },
        "public-contributor-journeys": {
            "workflow_run_id": 1,
            "journeys": ["bootstrap", "hooks"],
            "artifacts": evidence_candidate["artifacts"],
        },
        "public-user-journeys": {
            "workflow_run_id": 2,
            "journeys": ["wheel-install", "sdist-install"],
            "artifacts": evidence_candidate["artifacts"],
        },
        "documentation-rehearsals": {
            "rehearsal_evidence_sha256": "f" * 64,
            "verified_blocks": ["readme.md.block-1"],
            "scenarios": ["installed-base-artifact"],
        },
        "frontier-final-review": {
            "reviewed_revision": "a" * 40,
            "reviewer": "independent-reviewer",
            "model": "Claude Frontier",
            "review_mode": "fresh-eyes",
            "scope": "final release candidate",
            "unresolved_findings": [],
        },
        "cutover-approval": {
            "cutover_record_sha256": "a" * 64,
            "initial_commit": "a" * 40,
            "initial_tree": "b" * 40,
            "workflow_runs": [1],
        },
    }
    proof_sources = {
        "github-release-environment": "https://api.github.com/repos/mpeter/fieldkit-cli/environments/pypi",
        "pypi-trusted-publisher": "https://github.com/mpeter/fieldkit-cli/actions/workflows/release.yml",
        "package-name-reservation": "https://pypi.org/pypi/fieldkit-cli/json",
        "repository-controls": "https://api.github.com/repos/mpeter/fieldkit-cli",
        "testpypi-rehearsal": "https://test.pypi.org/pypi/fieldkit-cli/json",
        "public-contributor-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/1",
        "public-user-journeys": "https://github.com/mpeter/fieldkit-cli/actions/runs/2",
        "documentation-rehearsals": "https://github.com/mpeter/fieldkit-cli/actions/runs/3",
        "frontier-final-review": "https://github.com/mpeter/fieldkit-cli/pull/1#discussion_r1",
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
                    "captured_at": "2026-09-19T00:00:00Z",
                    "payload": payload,
                    "payload_sha256": release_check._canonical_json_sha256(payload),
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

    result = release_check._manual_criteria(Path(__file__).parents[1], evidence_path, candidate_path)

    criteria_result, ledger_sha256 = result
    assert [criterion.id for criterion in criteria_result] == list(required_ids)
    assert all(criterion.status == "pass" for criterion in criteria_result)
    assert ledger_sha256 == sha256(evidence_path.read_bytes()).hexdigest()


def test_manual_evidence_rejects_a_record_with_a_claimed_digest(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"
    record_path.write_text('{"schema_version": 1, "kind": "x", "status": "pass", "candidate": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="record digest"):
        release_check._manual_record_bytes(record_path, "0" * 64)


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
        release_check._validate_manual_record(
            "github-release-environment",
            {
                "schema_version": 1,
                "kind": "github-release-environment",
                "status": "pass",
                "candidate": candidate,
            },
            candidate,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_revision", "c" * 40),
        ("review_mode", "follow-up"),
        ("unresolved_findings", ["unresolved release issue"]),
    ],
)
def test_frontier_final_review_requires_exact_clean_fresh_eyes_result(field: str, value: object) -> None:
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload: dict[str, object] = {
        "reviewed_revision": "a" * 40,
        "reviewer": "independent-reviewer",
        "model": "Claude Frontier",
        "review_mode": "fresh-eyes",
        "scope": "final release candidate",
        "unresolved_findings": [],
    }
    payload[field] = value
    record = {
        "schema_version": 1,
        "kind": "frontier-final-review",
        "status": "pass",
        "candidate": candidate,
        "proof": {
            "source_uri": "https://github.com/mpeter/fieldkit-cli/pull/1#discussion_r1",
            "captured_at": "2026-09-19T00:00:00Z",
            "payload": payload,
            "payload_sha256": release_check._canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match="invalid typed proof values"):
        release_check._validate_manual_record("frontier-final-review", record, candidate)


@pytest.mark.parametrize("gate", release_check._REQUIRED_EVIDENCE_IDS)
def test_manual_evidence_rejects_null_typed_payload_values(gate: str) -> None:
    """Every external gate needs observations, not a shape-preserving null claim."""
    candidate = {
        "repository": "mpeter/fieldkit-cli",
        "source_sha": "a" * 40,
        "exported_tree": "b" * 40,
        "planned_tag": "v1.0.0",
        "artifacts": [],
    }
    payload = dict.fromkeys(release_check._RECORD_PAYLOAD_FIELDS[gate])
    record = {
        "schema_version": 1,
        "kind": gate,
        "status": "pass",
        "candidate": candidate,
        "proof": {
            "source_uri": release_check._RECORD_SOURCE_PREFIXES[gate][0] + "evidence",
            "captured_at": "2026-09-19T00:00:00Z",
            "payload": payload,
            "payload_sha256": release_check._canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match="invalid typed proof values"):
        release_check._validate_manual_record(gate, record, candidate)


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
            "payload_sha256": release_check._canonical_json_sha256(payload),
        },
    }

    with pytest.raises(ValueError, match="invalid capture time"):
        release_check._validate_manual_record("github-release-environment", record, candidate)


def test_manual_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    evidence_path = tmp_path / "manual-evidence.json"
    evidence_path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key: schema_version"):
        release_check._load_json(evidence_path)


def test_manual_record_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"
    record_bytes = b'{"schema_version": 1, "schema_version": 1}'
    record_path.write_bytes(record_bytes)

    with pytest.raises(ValueError, match="duplicate JSON key: schema_version"):
        release_check._manual_record_bytes(record_path, sha256(record_bytes).hexdigest())


def test_stale_revision_writes_failure_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(release_check, "_require_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(release_check, "_head_revision", lambda _repo: "b" * 40)
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
        release_check, "_require_clean_worktree", lambda _repo: (_ for _ in ()).throw(ValueError("clean worktree"))
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
