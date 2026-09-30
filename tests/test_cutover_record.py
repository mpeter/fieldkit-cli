"""Contracts for the exact-candidate public cutover record."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import _release_bundle_evidence, cutover_record

pytestmark = pytest.mark.unit


def test_cutover_document_rejects_symlink_without_following_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_bytes(b"{}")
    redirect = tmp_path / "record.json"
    redirect.symlink_to(target)
    with pytest.raises(ValueError, match=r"^cutover JSON document is unavailable or exceeds its byte limit$"):
        cutover_record._load(redirect)
    assert target.read_bytes() == b"{}"


def test_cutover_document_enforces_canonical_reader_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    boundary = b"{}" + b" " * (_release_bundle_evidence._MAX_FILE_BYTES - 2)
    path.write_bytes(boundary)
    result = cutover_record._load(path)
    assert result == {}
    path.write_bytes(boundary + b" ")
    with pytest.raises(ValueError, match=r"^cutover JSON document is unavailable or exceeds its byte limit$"):
        cutover_record._load(path)


@pytest.mark.parametrize("case", ["control_key", "long_key", "nested"])
def test_cutover_cli_json_failure_is_fixed_without_traceback(tmp_path: Path, case: str) -> None:
    key = "synthetic-private-key\n\x1b[31m" if case == "control_key" else "synthetic-private-key" + "x" * 8192
    encoded = json.dumps(key)
    raw = (
        ("{" + encoded + ":1," + encoded + ":2}").encode()
        if case != "nested"
        else ("[" * 5000 + "0" + "]" * 5000).encode()
    )
    record = tmp_path / "record.json"
    record.write_bytes(raw)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.cutover_record",
            "--record",
            str(record),
            "--candidate-report",
            str(record),
            "--controller-root",
            str(Path(__file__).parents[1]),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Cutover record: ERROR: cutover JSON document is invalid JSON\n"
    assert record.read_bytes() == raw


def test_decoded_cutover_record_schema_diagnostic_never_reflects_value() -> None:
    record = _record()
    record["schema_version"] = "synthetic-private-value\n\x1b[31m" + "x" * 8192
    with pytest.raises(ValueError) as caught:
        cutover_record.validate(record, _candidate(), controller_root=Path(__file__).parents[1])
    assert str(caught.value) == "cutover record schema violation at properties.schema_version.const: const"


def test_decoded_cutover_record_rejects_excessive_nesting() -> None:
    nested: object = 0
    for _ in range(65):
        nested = [nested]
    record = _record()
    record["schema_version"] = nested
    with pytest.raises(ValueError, match=r"^JSON input exceeds nesting limit$"):
        cutover_record.validate(record, _candidate(), controller_root=Path(__file__).parents[1])


def _candidate() -> dict[str, object]:
    return {
        "status": "pass",
        "expected_repository": "mpeter/fieldkit-cli",
        "planned_tag": "v1.0.0",
        "export_manifest": {
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "policy_sha256": "c" * 64,
            "exported_tree": "d" * 40,
        },
        "artifact_validation": {
            "source_revision": "a" * 40,
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "kind": "wheel", "sha256": "e" * 64, "status": "pass"},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": "f" * 64, "status": "pass"},
            ],
        },
    }


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": {
            "repository": "mpeter/fieldkit-cli",
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "export_policy_sha256": "c" * 64,
            "exported_tree": "d" * 40,
            "planned_tag": "v1.0.0",
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "kind": "wheel", "sha256": "e" * 64},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": "f" * 64},
            ],
        },
        "public": {
            "initial_commit": "a" * 40,
            "initial_tree": "d" * 40,
            "repository_id": 1,
            "workflow_runs": [
                {
                    "name": "Cutover verification",
                    "path": ".github/workflows/cutover.yml",
                    "event": "push",
                    "id": 2,
                    "attempt": 1,
                    "head_sha": "a" * 40,
                    "conclusion": "success",
                }
            ],
        },
    }


def test_validates_a_complete_record_bound_to_the_candidate() -> None:
    cutover_record.validate(_record(), _candidate(), controller_root=Path(__file__).parents[1])


def test_rejects_a_record_for_another_exported_tree() -> None:
    record = deepcopy(_record())
    candidate = record["candidate"]
    assert isinstance(candidate, dict)
    candidate["exported_tree"] = "9" * 40

    with pytest.raises(ValueError, match="exported tree"):
        cutover_record.validate(record, _candidate(), controller_root=Path(__file__).parents[1])


def test_rejects_a_record_without_successful_ci_for_the_initial_commit() -> None:
    record = deepcopy(_record())
    public = record["public"]
    assert isinstance(public, dict)
    runs = public["workflow_runs"]
    assert isinstance(runs, list)
    run = runs[0]
    assert isinstance(run, dict)
    run["head_sha"] = "9" * 40

    with pytest.raises(ValueError, match="root-commit verification workflow run"):
        cutover_record.validate(record, _candidate(), controller_root=Path(__file__).parents[1])


def test_resolves_the_recorded_public_identity_from_the_fresh_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    (tmp_path / ".git").mkdir()

    def command(repository: Path, argv: list[str]) -> str:
        assert repository == tmp_path
        if argv[:3] == ["git", "remote", "get-url"]:
            return "https://github.com/mpeter/fieldkit-cli.git"
        if argv[0:2] == ["git", "rev-parse"] and "^{commit}" in argv[-1]:
            return "a" * 40
        if argv[0:2] == ["git", "rev-parse"]:
            if argv[-1] == "refs/remotes/origin/main":
                return "a" * 40
            return "d" * 40
        if argv[0:2] == ["git", "rev-list"]:
            return "a" * 40
        if argv == ["gh", "api", "repos/mpeter/fieldkit-cli", "--jq", ".id"]:
            return "1"
        assert argv == ["gh", "api", "repos/mpeter/fieldkit-cli/actions/runs/2"]
        return json.dumps(
            {
                "id": 2,
                "run_attempt": 1,
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": "success",
                "name": "Cutover verification",
                "event": "push",
                "path": ".github/workflows/cutover.yml@refs/heads/main",
            }
        )

    monkeypatch.setattr(cutover_record, "_command", command)

    cutover_record.verify_public_identity(record, tmp_path)
