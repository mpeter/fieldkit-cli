"""Contracts for retained release-approval inputs."""

import hashlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import (
    cutover_record,
    release_approval_input,
    release_bundle,
    release_filesystem,
    release_manual_evidence,
)
from tests import release_approval_support

pytestmark = pytest.mark.unit


def test_verify_requires_explicit_controller_and_expectations(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"controller_root.*expected"):
        release_approval_input.verify(tmp_path)  # type: ignore[call-arg]


@pytest.mark.parametrize("member", ["release-approval-input.schema.json", "public-tree-policy.json"])
def test_subject_control_files_cannot_override_selected_controller(tmp_path: Path, member: str) -> None:
    root = tmp_path / "subject"
    release_approval_support.write_input(root)
    expected = release_approval_support.expected_input(root)
    hostile = root / "docs/release-readiness" / member
    hostile.parent.mkdir(parents=True)
    hostile.write_text(json.dumps({"additionalProperties": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="unlisted"):
        release_approval_input.verify(root, controller_root=Path(__file__).parents[1], expected=expected)


@pytest.mark.parametrize("control", ["schema", "policy"])
def test_selected_controller_controls_validation_despite_hostile_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    root = tmp_path / "subject"
    release_approval_support.write_input(root)
    expected = release_approval_support.expected_input(root)
    controller = tmp_path / "controller"
    policies = controller / "docs/release-readiness"
    policies.mkdir(parents=True)
    original = Path(__file__).parents[1] / "docs/release-readiness"
    schema = json.loads((original / "release-approval-input.schema.json").read_bytes())
    policy = json.loads((original / "public-tree-policy.json").read_bytes())
    if control == "schema":
        schema["properties"]["schema_version"] = {"const": 99}
    else:
        policy["expected_repository"] = "other/fieldkit-cli"
    (policies / "release-approval-input.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (policies / "public-tree-policy.json").write_text(json.dumps(policy), encoding="utf-8")
    hostile = tmp_path / "candidate-checkout/docs/release-readiness"
    hostile.mkdir(parents=True)
    (hostile / "release-approval-input.schema.json").write_text("{}", encoding="utf-8")
    (hostile / "public-tree-policy.json").write_bytes((original / "public-tree-policy.json").read_bytes())
    monkeypatch.chdir(hostile.parent.parent)
    with pytest.raises(
        ValueError, match="schema violation" if control == "schema" else "independently selected digest"
    ):
        release_approval_input.verify(root, controller_root=controller, expected=expected)


def test_policy_whitespace_substitution_cannot_rebind_selected_digest(tmp_path: Path) -> None:
    root = tmp_path / "subject"
    release_approval_support.write_input(root)
    expected = release_approval_support.expected_input(root)
    controller = tmp_path / "controller"
    policies = controller / "docs/release-readiness"
    policies.mkdir(parents=True)
    original = Path(__file__).parents[1] / "docs/release-readiness"
    (policies / "release-approval-input.schema.json").write_bytes(
        (original / "release-approval-input.schema.json").read_bytes()
    )
    original_policy = (original / "public-tree-policy.json").read_bytes()
    substituted_policy = original_policy + b"\n"
    assert json.loads(substituted_policy) == json.loads(original_policy)
    (policies / "public-tree-policy.json").write_bytes(substituted_policy)
    names = ("private-candidate-report.json", "public-candidate/report.json")
    for name in names:
        report_path = root / name
        report = json.loads(report_path.read_bytes())
        report["export_manifest"]["policy_sha256"] = hashlib.sha256(substituted_policy).hexdigest()
        report_path.write_text(json.dumps(report), encoding="utf-8")
    _refresh_manifest_members(root, *names)
    with pytest.raises(ValueError, match="independently selected digest"):
        release_approval_input.verify(root, controller_root=controller, expected=expected)


def test_policy_digest_must_match_independent_selection(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    expected = replace(release_approval_support.expected_input(tmp_path), public_tree_policy_sha256="0" * 64)
    with pytest.raises(ValueError, match="independently selected digest"):
        release_approval_input.verify(tmp_path, controller_root=Path(__file__).parents[1], expected=expected)


@pytest.mark.parametrize("name", ["private-candidate-report.json", "public-candidate/report.json"])
@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_each_report_requires_selected_controller_policy_digest(tmp_path: Path, name: str, digest: str | None) -> None:
    release_approval_support.write_input(tmp_path)
    expected = release_approval_support.expected_input(tmp_path)
    report_path = tmp_path / name
    report = json.loads(report_path.read_bytes())
    if digest is None:
        del report["export_manifest"]["policy_sha256"]
    else:
        report["export_manifest"]["policy_sha256"] = digest
    report_path.write_text(json.dumps(report), encoding="utf-8")
    _refresh_manifest_members(tmp_path, name)
    with pytest.raises(ValueError, match="policy digests are not bound"):
        release_approval_input.verify(tmp_path, controller_root=Path(__file__).parents[1], expected=expected)


@pytest.mark.parametrize("digest", [None, "", "A" * 64, "a" * 63, "g" * 64, 7])
def test_expected_selection_requires_lowercase_policy_sha256(tmp_path: Path, digest: object) -> None:
    release_approval_support.write_input(tmp_path)
    selection_path = release_approval_support.write_selection(tmp_path)
    selection = json.loads(selection_path.read_bytes())
    if digest is None:
        del selection["public_tree_policy_sha256"]
    else:
        selection["public_tree_policy_sha256"] = digest
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    with pytest.raises(ValueError, match=r"expected selection fields|expected public tree policy digest"):
        release_approval_input._expected_selection(selection_path)


def test_expected_selection_retains_matching_policy_digest(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    selected = release_approval_input._expected_selection(release_approval_support.write_selection(tmp_path))
    assert selected == release_approval_support.expected_input(tmp_path)
    assert (
        selected.public_tree_policy_sha256
        == hashlib.sha256(
            (Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json").read_bytes()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "identity",
    [
        "repository",
        "private_source_sha",
        "private_source_tree",
        "exported_tree",
        "public_commit",
        "planned_tag",
        "artifact",
    ],
)
def test_rehashed_subject_substitution_cannot_change_selected_identity(tmp_path: Path, identity: str) -> None:
    release_approval_support.write_input(tmp_path)
    expected = release_approval_support.expected_input(tmp_path)
    original = expected.artifacts[0][2] if identity == "artifact" else getattr(expected, identity)
    substituted = (
        "other/fieldkit-cli"
        if identity == "repository"
        else "v2.0.0"
        if identity == "planned_tag"
        else "8" * len(original)
    )
    names = [
        "private-candidate-report.json",
        "public-candidate/report.json",
        "cutover-record.json",
        "evidence/ledger.json",
    ]
    for name in [*names, "approval-manifest.json"]:
        member = tmp_path / name
        member.write_bytes(member.read_bytes().replace(original.encode(), substituted.encode()))
    _refresh_manifest_members(tmp_path, *names)
    with pytest.raises(ValueError, match="independently selected"):
        release_approval_input.verify(tmp_path, controller_root=Path(__file__).parents[1], expected=expected)


@pytest.mark.parametrize("missing", ["--controller-root", "--expected-selection"])
def test_approval_cli_requires_explicit_selection(tmp_path: Path, missing: str) -> None:
    argv = ["--input", str(tmp_path)]
    if missing != "--controller-root":
        argv.extend(["--controller-root", str(tmp_path)])
    if missing != "--expected-selection":
        argv.extend(["--expected-selection", str(tmp_path / "selected.json")])
    with pytest.raises(SystemExit) as caught:
        release_approval_input.main(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("contract_matches", [True, False])
def test_manual_byte_helper_separates_controller_policy_from_subject_contract(
    tmp_path: Path, contract_matches: bool
) -> None:
    root = tmp_path / "subject"
    release_approval_support.write_real_input(root)
    expected = release_approval_support.expected_input(root)
    controller = tmp_path / "controller"
    policies = controller / "docs/release-readiness"
    policies.mkdir(parents=True)
    original = Path(__file__).parents[1] / "docs/release-readiness"
    for name in (
        "release-governance-policy.json",
        "release-manual-evidence.schema.json",
        "release-evidence-record.schema.json",
        "rehearsal-evidence.schema.json",
        "release-consumer-evidence.schema.json",
    ):
        (policies / name).write_bytes((original / name).read_bytes())
    evidence = root / "evidence"
    ledger_bytes = (evidence / "ledger.json").read_bytes()
    records = {
        path.relative_to(evidence).as_posix(): path.read_bytes()
        for path in evidence.rglob("*")
        if path.is_file() and path.name != "ledger.json"
    }
    artifacts = {
        name: (root / "public-candidate/bundle" / name).read_bytes() for name, _kind, _digest in expected.artifacts
    }
    with (
        nullcontext()
        if contract_matches
        else pytest.raises(ValueError, match="does not bind the retained public candidate")
    ):
        criteria, digest = release_manual_evidence.validate_bytes(
            controller,
            documentation_contract_sha256=expected.documentation_contract_sha256 if contract_matches else "1" * 64,
            evidence_bytes=ledger_bytes,
            candidate_report_bytes=(root / "public-candidate/report.json").read_bytes(),
            private_candidate_report_bytes=(root / "private-candidate-report.json").read_bytes(),
            record_bytes=records,
            artifact_bytes=artifacts,
        )
        assert digest == hashlib.sha256(ledger_bytes).hexdigest()
        assert any(criterion.status == "pending" for criterion in criteria)


@pytest.mark.parametrize(
    "field",
    [
        "repository",
        "private_source_sha",
        "private_source_tree",
        "exported_tree",
        "public_commit",
        "public_tree",
        "planned_tag",
        "artifacts",
    ],
)
def test_independent_selection_rejects_consistently_self_bound_subject(tmp_path: Path, field: str) -> None:
    release_approval_support.write_input(tmp_path)
    expected = release_approval_support.expected_input(tmp_path)
    values = {
        "repository": "other/fieldkit-cli",
        "private_source_sha": "1" * 40,
        "private_source_tree": "2" * 40,
        "exported_tree": "3" * 40,
        "public_commit": "4" * 40,
        "public_tree": "5" * 40,
        "planned_tag": "v2.0.0",
    }
    selected = replace(
        expected,
        repository=values["repository"] if field == "repository" else expected.repository,
        private_source_sha=values["private_source_sha"]
        if field == "private_source_sha"
        else expected.private_source_sha,
        private_source_tree=values["private_source_tree"]
        if field == "private_source_tree"
        else expected.private_source_tree,
        exported_tree=values["exported_tree"] if field == "exported_tree" else expected.exported_tree,
        public_commit=values["public_commit"] if field == "public_commit" else expected.public_commit,
        public_tree=values["public_tree"] if field == "public_tree" else expected.public_tree,
        planned_tag=values["planned_tag"] if field == "planned_tag" else expected.planned_tag,
        artifacts=(("other.whl", "wheel", "6" * 64), ("other.tar.gz", "sdist", "7" * 64))
        if field == "artifacts"
        else expected.artifacts,
    )
    with pytest.raises(ValueError, match="independently selected"):
        release_approval_input.verify(tmp_path, controller_root=Path(__file__).parents[1], expected=selected)


def test_approval_cli_cannot_promote_structural_validation_to_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: list[Path] = []

    release_approval_support.write_input(tmp_path)
    selection = release_approval_support.write_selection(tmp_path)

    def structurally_valid(
        root: Path, *, controller_root: Path, expected: release_approval_input.ExpectedApprovalIdentity
    ) -> dict[str, object]:
        observed.append(root)
        return {"status": "pass"}

    monkeypatch.setattr(release_approval_input, "verify", structurally_valid)

    result = release_approval_input.main(
        [
            "--input",
            str(tmp_path),
            "--controller-root",
            str(Path(__file__).parents[1]),
            "--expected-selection",
            str(selection),
        ]
    )

    assert result == 1
    assert "Structural approval input validation does not grant release authority." in (
        Path(__file__).parents[1] / "RELEASING.md"
    ).read_text(encoding="utf-8")
    assert observed == [tmp_path]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Release approval input: PENDING: structural validation completed; "
        "independent controller and release authority are not authenticated\n"
    )


@pytest.mark.parametrize("case", ["control_key", "long_key", "nested"])
def test_approval_cli_json_failure_has_fixed_diagnostic(tmp_path: Path, case: str) -> None:
    key = "synthetic-private-key\n\x1b[31m" if case == "control_key" else "synthetic-private-key" + "x" * 8192
    encoded = json.dumps(key)
    raw = (
        ("{" + encoded + ":1," + encoded + ":2}").encode()
        if case != "nested"
        else ("[" * 5000 + "0" + "]" * 5000).encode()
    )
    release_approval_support.write_input(tmp_path)
    selection = release_approval_support.write_selection(tmp_path)
    (tmp_path / "approval-manifest.json").write_bytes(raw)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release_approval_input",
            "--input",
            str(tmp_path),
            "--controller-root",
            str(Path(__file__).parents[1]),
            "--expected-selection",
            str(selection),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Release approval input: ERROR: invalid approval input JSON: approval-manifest.json\n"


def test_verify_revalidates_every_retained_release_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_approval_support.write_input(tmp_path)
    observed: list[str] = []
    policy_reads: list[bytes] = []
    original_read = release_approval_input.read_root_bytes

    def read_controller_bytes(root: Path, name: str) -> bytes:
        data = original_read(root, name)
        if name == "public-tree-policy.json":
            policy_reads.append(data)
        return data

    def verified_criteria(
        *_args: object, **_kwargs: object
    ) -> tuple[tuple[release_manual_evidence.Criterion, ...], str]:
        observed.append("ledger")
        return (release_manual_evidence.Criterion("component-routing-fixture", "pass", "fixture"),), "a" * 64

    def verify_bundle(*_args: object, **_kwargs: object) -> dict[str, bytes]:
        observed.append("bundle")
        return {"fieldkit.whl": b"fixture-wheel", "fieldkit.tar.gz": b"fixture-sdist"}

    monkeypatch.setattr(release_manual_evidence, "validate_bytes", verified_criteria)
    monkeypatch.setattr(release_approval_input, "verified_bundle_artifacts", verify_bundle)
    monkeypatch.setattr(cutover_record, "validate", lambda *_args, **_kwargs: observed.append("cutover"))
    monkeypatch.setattr(release_approval_input, "_cutover_record_digest", lambda *_args: observed.append("binding"))
    monkeypatch.setattr(release_approval_input, "read_root_bytes", read_controller_bytes)

    assert (
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )["status"]
        == "pass"
    )
    assert observed == ["bundle", "ledger", "cutover", "binding"]
    assert policy_reads == [(Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json").read_bytes()]


def test_verify_keeps_manual_rehearsals_nonpassing_without_behavioral_verifiers(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)

    with pytest.raises(ValueError, match="pending or nonpassing rehearsal criteria"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_approval_rejects_pending_criteria_after_verifying_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    release_approval_support.write_real_input(tmp_path)
    events: list[str] = []
    original_verify = release_bundle.capture_open_bundle

    def verified_bundle(*args: object, **kwargs: object) -> release_bundle.VerifiedBundle[release_bundle.BundleReport]:
        events.append("bundle")
        # The real bundle verifier, not a success stub, must run before criteria.
        assert len(args) == 1 and isinstance(args[0], int)
        report = kwargs["candidate_report"]
        checksums = kwargs["expected_checksums"]
        assert isinstance(report, bytes) and isinstance(checksums, bytes)
        return original_verify(args[0], candidate_report=report, expected_checksums=checksums)

    def pending_criteria(*args: object, **kwargs: object) -> tuple[tuple[release_manual_evidence.Criterion, ...], str]:
        events.append("criteria")
        return (
            release_manual_evidence.Criterion("documentation-rehearsals", "pending", "unapproved controller"),
        ), "a" * 64

    monkeypatch.setattr(release_bundle, "capture_open_bundle", verified_bundle)
    monkeypatch.setattr(release_manual_evidence, "validate_bytes", pending_criteria)

    assert (
        release_approval_input.main(
            [
                "--input",
                str(tmp_path),
                "--controller-root",
                str(Path(__file__).parents[1]),
                "--expected-selection",
                str(release_approval_support.write_selection(tmp_path)),
            ]
        )
        == 2
    )
    assert events == ["bundle", "criteria"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pending" in captured.err


@pytest.mark.parametrize("change", ["tampered", "missing", "unreferenced"])
def test_rehearsal_catalog_and_outer_manifest_both_remain_closed(tmp_path: Path, change: str) -> None:
    release_approval_support.write_real_input(tmp_path)
    name = "evidence/streams/empty.txt"
    path = tmp_path / name
    if change == "tampered":
        path.write_bytes(b"changed retained stream")
        _refresh_manifest_members(tmp_path, name)
        diagnostic = "digest or size"
    else:
        manifest_path = tmp_path / "approval-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if change == "missing":
            path.unlink()
            manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != name]
            diagnostic = "catalog is incomplete"
        else:
            extra_name = "evidence/support/unreferenced.txt"
            (tmp_path / extra_name).write_bytes(b"extra")
            manifest["files"].append({"path": extra_name, "sha256": hashlib.sha256(b"extra").hexdigest()})
            diagnostic = "unreferenced record bytes"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=diagnostic):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def _refresh_manifest_members(root: Path, *names: str) -> None:
    manifest_path = root / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] in names:
            entry["sha256"] = hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_verify_rejects_private_and_cutover_artifacts_that_disagree_with_the_public_candidate(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)
    replacement = "0" * 64
    private_path = tmp_path / "private-candidate-report.json"
    private = json.loads(private_path.read_text(encoding="utf-8"))
    private["artifact_validation"]["artifacts"][0]["sha256"] = replacement
    private_path.write_text(json.dumps(private, sort_keys=True), encoding="utf-8")

    cutover_path = tmp_path / "cutover-record.json"
    cutover = json.loads(cutover_path.read_text(encoding="utf-8"))
    cutover["candidate"]["artifacts"][0]["sha256"] = replacement
    cutover_path.write_text(json.dumps(cutover, sort_keys=True), encoding="utf-8")

    ledger_path = tmp_path / "evidence/ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    criterion = next(item for item in ledger["criteria"] if item["id"] == "cutover-approval")
    record_path = tmp_path / "evidence" / criterion["record"]["path"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    payload = record["proof"]["payload"]
    payload["cutover_record_sha256"] = hashlib.sha256(cutover_path.read_bytes()).hexdigest()
    record["proof"]["payload_sha256"] = release_manual_evidence.canonical_json_sha256(payload)
    record_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    criterion["record"]["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    ledger_path.write_text(json.dumps(ledger, sort_keys=True), encoding="utf-8")
    _refresh_manifest_members(
        tmp_path,
        "private-candidate-report.json",
        "cutover-record.json",
        "evidence/ledger.json",
        f"evidence/{criterion['record']['path']}",
    )

    with pytest.raises(ValueError, match="artifacts do not match"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


@pytest.mark.parametrize("relative_path", ["unlisted-secret.txt", "evidence/support/unlisted-secret.txt"])
def test_verify_rejects_unlisted_regular_members(tmp_path: Path, relative_path: str) -> None:
    release_approval_support.write_real_input(tmp_path)
    extra = tmp_path / relative_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("not retained", encoding="utf-8")

    with pytest.raises(ValueError, match="unlisted"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_verify_rejects_an_unlisted_empty_directory(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)
    (tmp_path / "evidence/unlisted-directory").mkdir()

    with pytest.raises(ValueError, match="unlisted"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_verify_rejects_an_unknown_member_even_when_the_manifest_lists_its_digest(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)
    unknown = tmp_path / "unknown-private-input.txt"
    unknown.write_text("not a release input", encoding="utf-8")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": unknown.name, "sha256": hashlib.sha256(unknown.read_bytes()).hexdigest()})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown member"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo"])
def test_verify_rejects_unlisted_unsafe_members(
    tmp_path: Path, unsafe_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    release_approval_support.write_real_input(tmp_path)
    selection = release_approval_support.write_selection(tmp_path)
    unsafe = tmp_path / "private-name-sentinel\x1b[31m"
    if unsafe_kind == "symlink":
        unsafe.symlink_to(tmp_path / "private-candidate-report.json")
    else:
        os.mkfifo(unsafe)

    with pytest.raises(ValueError, match="unsafe") as captured:
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )
    assert str(captured.value) == "approval input contains an unsafe retained member"
    assert (
        release_approval_input.main(
            [
                "--input",
                str(tmp_path),
                "--controller-root",
                str(Path(__file__).parents[1]),
                "--expected-selection",
                str(selection),
            ]
        )
        == 2
    )
    assert capsys.readouterr().err == (
        "Release approval input: ERROR: approval input contains an unsafe retained member\n"
    )


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("depth", [64, 65, 66])
def test_retained_tree_bounds_member_depth(tmp_path: Path, kind: str, depth: int) -> None:
    member = tmp_path.joinpath(*(["d"] * depth))
    if kind == "directory":
        member.mkdir(parents=True)
    else:
        member.parent.mkdir(parents=True)
        member.write_bytes(b"retained")
    root_fd = release_approval_input._open_real_directory(tmp_path)
    try:
        if depth > 64:
            with pytest.raises(ValueError, match=r"^approval input member exceeds its depth limit$"):
                release_approval_input._retained_tree(root_fd)
        else:
            result = release_approval_input._retained_tree(root_fd)
            relative = member.relative_to(tmp_path).as_posix()
            assert result[0] == ({relative} if kind == "file" else set())
            assert len(result[1]) == depth - (kind == "file")
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("reader", ["_safe_bytes", "_open_member_directory"])
def test_member_traversal_rejects_excessive_depth_before_opening(tmp_path: Path, reader: str) -> None:
    root_fd = release_approval_input._open_real_directory(tmp_path)
    try:
        with pytest.raises(ValueError, match=r"^approval input member exceeds its depth limit$"):
            getattr(release_approval_input, reader)(root_fd, "/".join(["d"] * 65))
    finally:
        os.close(root_fd)


def test_approval_cli_rejects_excessive_retained_depth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    release_approval_support.write_real_input(tmp_path)
    selection = release_approval_support.write_selection(tmp_path)
    tmp_path.joinpath(*(["d"] * 65)).mkdir(parents=True)

    assert (
        release_approval_input.main(
            [
                "--input",
                str(tmp_path),
                "--controller-root",
                str(Path(__file__).parents[1]),
                "--expected-selection",
                str(selection),
            ]
        )
        == 2
    )
    assert capsys.readouterr().err == ("Release approval input: ERROR: approval input member exceeds its depth limit\n")


@pytest.mark.parametrize(
    ("reader", "diagnostic"),
    [
        ("_safe_bytes", "approval input member is unavailable or exceeds its byte limit"),
        ("_open_member_directory", "approval input directory is unavailable"),
    ],
)
def test_unavailable_member_diagnostics_omit_untrusted_names(tmp_path: Path, reader: str, diagnostic: str) -> None:
    root_fd = release_approval_input._open_real_directory(tmp_path)
    try:
        with pytest.raises(ValueError) as captured:
            getattr(release_approval_input, reader)(root_fd, "private-name-sentinel\x1b[31m")
        assert str(captured.value) == diagnostic
    finally:
        os.close(root_fd)


def test_digest_failure_diagnostic_omits_untrusted_member_name(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    name = "evidence/private-name-sentinel.json"
    (tmp_path / name).write_bytes(b"retained")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"].append({"path": name, "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError) as captured:
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )
    assert str(captured.value) == "approval input digest does not match"


@pytest.mark.parametrize("operation", ["listdir", "stat", "open_directory"])
def test_retained_tree_controls_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    (tmp_path / "private-name-sentinel\x1b[31m").mkdir()
    root_fd = release_approval_input._open_real_directory(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("private-name-sentinel\x1b[31m")

    if operation == "open_directory":
        monkeypatch.setattr(release_filesystem, "open_directory_at", fail)
    else:
        monkeypatch.setattr(os, operation, fail)
    try:
        with pytest.raises(ValueError) as captured:
            release_approval_input._retained_tree(root_fd)
        assert str(captured.value) == (
            "approval input retained member is unavailable"
            if operation == "stat"
            else "approval input retained directory is unavailable"
        )
    finally:
        os.close(root_fd)


def test_verify_rejects_substituted_member(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    (tmp_path / "evidence/ledger.json").write_text('{"substituted": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="digest does not match"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_verify_rejects_public_candidate_from_another_cutover(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    report = json.loads((tmp_path / "public-candidate/report.json").read_text(encoding="utf-8"))
    report["export_manifest"]["source_commit"] = "f" * 40
    path = tmp_path / "public-candidate/report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "public-candidate/report.json":
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identities are not bound"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_verify_rejects_evidence_record_not_listed_in_manifest(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    ledger_path = tmp_path / "evidence/ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["criteria"][-1]["record"]["path"] = "unretained.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "evidence/ledger.json":
            entry["sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="does not retain every evidence record"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_verify_rejects_missing_required_checksums_even_with_an_extra_member(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    extra = tmp_path / "extra.json"
    extra.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "public-candidate/bundle/SHA256SUMS"]
    manifest["files"].append({"path": "extra.json", "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="every required member"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("repository", "other/fieldkit-cli"), ("planned_tag", "v2.0.0")],
)
def test_verify_rejects_manifest_candidate_repository_or_tag_mismatch(tmp_path: Path, field: str, value: str) -> None:
    release_approval_support.write_input(tmp_path)
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="repository and planned tag"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_cutover_binding_uses_the_manifest_verified_ledger_record_bytes(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"cutover").hexdigest()
    retained = json.dumps({"proof": {"payload": {"cutover_record_sha256": expected}}}).encode()
    swapped = json.dumps({"proof": {"payload": {"cutover_record_sha256": "0" * 64}}}).encode()
    (tmp_path / "record.json").write_bytes(swapped)
    ledger = {"criteria": [{"id": "cutover-approval", "record": {"path": "record.json"}}]}

    release_approval_input._cutover_record_digest(ledger, {"record.json": retained}, expected)


def test_evidence_map_includes_digest_verified_support_members() -> None:
    ledger = {"criteria": [{"id": "cutover-approval", "record": {"path": "records/cutover.json"}}]}
    members = {
        "evidence/ledger.json": b"ledger",
        "evidence/records/cutover.json": b"record",
        "evidence/support/review-response.json": b"response",
        "public-candidate/report.json": b"candidate",
    }

    assert release_approval_input._record_bytes(ledger, members) == {
        "records/cutover.json": b"record",
        "support/review-response.json": b"response",
    }


def test_verify_does_not_reread_an_evidence_member_after_digest_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_approval_support.write_input(tmp_path)
    cutover_bytes = (tmp_path / "cutover-record.json").read_bytes()
    expected = hashlib.sha256(cutover_bytes).hexdigest()
    record_path = tmp_path / "evidence" / "record.json"
    retained = json.dumps({"proof": {"payload": {"cutover_record_sha256": expected}}}).encode()
    record_path.write_bytes(retained)
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "evidence/record.json":
            entry["sha256"] = hashlib.sha256(retained).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def validate_bytes(
        *_args: object, record_bytes: dict[str, bytes], **_kwargs: object
    ) -> tuple[tuple[release_manual_evidence.Criterion, ...], str]:
        assert record_bytes == {
            **{
                f"{identifier}.json": b"{}"
                for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
                if identifier != "cutover-approval"
            },
            "record.json": retained,
        }
        record_path.write_text('{"mutated": true}', encoding="utf-8")
        return (release_manual_evidence.Criterion("retained-reader-fixture", "pass", "fixture"),), "a" * 64

    monkeypatch.setattr(release_manual_evidence, "validate_bytes", validate_bytes)
    monkeypatch.setattr(
        release_approval_input,
        "verified_bundle_artifacts",
        lambda *_args, **_kwargs: {"fieldkit.whl": b"fixture-wheel", "fieldkit.tar.gz": b"fixture-sdist"},
    )
    monkeypatch.setattr(cutover_record, "validate", lambda *_args, **_kwargs: None)

    assert (
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )["status"]
        == "pass"
    )


def test_verify_rejects_an_input_root_reached_through_a_symlinked_ancestor(tmp_path: Path) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    root = actual_parent / "input"
    root.mkdir()
    release_approval_support.write_input(root)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory path"):
        release_approval_input.verify(
            linked_parent / "input",
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(linked_parent / "input"),
        )


def test_verify_keeps_using_the_open_root_if_an_ancestor_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    release_approval_support.write_input(root)
    pinned = tmp_path / "pinned"
    attacker = tmp_path / "attacker"
    original_safe_bytes = release_approval_input._safe_bytes
    swapped = False

    def safe_bytes(root_fd: int, name: object) -> bytes:
        nonlocal swapped
        data = original_safe_bytes(root_fd, name)
        if not swapped:
            root.rename(pinned)
            attacker.mkdir()
            root.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return data

    monkeypatch.setattr(release_approval_input, "_safe_bytes", safe_bytes)
    monkeypatch.setattr(
        release_manual_evidence,
        "validate_bytes",
        lambda *_a, **_k: ((release_manual_evidence.Criterion("pinned-root-fixture", "pass", "fixture"),), "a" * 64),
    )
    monkeypatch.setattr(
        release_approval_input,
        "verified_bundle_artifacts",
        lambda *_a, **_k: {"fieldkit.whl": b"fixture-wheel", "fieldkit.tar.gz": b"fixture-sdist"},
    )
    monkeypatch.setattr(cutover_record, "validate", lambda *_a, **_k: None)
    monkeypatch.setattr(release_approval_input, "_cutover_record_digest", lambda *_a, **_k: None)

    assert (
        release_approval_input.verify(
            root, controller_root=Path(__file__).parents[1], expected=release_approval_support.expected_input(root)
        )["status"]
        == "pass"
    )


def test_verify_rejects_an_oversized_approval_manifest(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    expected = release_approval_support.expected_input(tmp_path)
    (tmp_path / "approval-manifest.json").write_bytes(b" " * (release_approval_input._MAX_APPROVAL_MEMBER_BYTES + 1))

    with pytest.raises(ValueError, match="byte limit"):
        release_approval_input.verify(tmp_path, controller_root=Path(__file__).parents[1], expected=expected)


def test_verify_rejects_an_oversized_manifest_member(tmp_path: Path) -> None:
    release_approval_support.write_input(tmp_path)
    record_path = tmp_path / "evidence" / "record.json"
    record_path.write_bytes(b" " * (release_approval_input._MAX_APPROVAL_MEMBER_BYTES + 1))
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "evidence/record.json":
            entry["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="byte limit"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )
