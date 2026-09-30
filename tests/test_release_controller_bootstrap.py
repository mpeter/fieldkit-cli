"""Synthetic host policy establishes byte agreement, never release authority."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import release_controller_bootstrap as bootstrap
from scripts import release_controller_closure as closure
from scripts import release_filesystem
from tests.test_release_trust_contracts import fixture_selection, mapping

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 30, tzinfo=UTC)
SUBPROCESS_TIMEOUT_SECONDS = 15
ROOT = Path(__file__).parents[1]


def encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def deployment() -> bootstrap.BootstrapDeploymentPolicy:
    schema_root = ROOT / "docs/release-readiness"
    return bootstrap.BootstrapDeploymentPolicy(
        (schema_root / "release-trust-selection.schema.json").read_bytes(),
        (schema_root / "release-controller-receipt.schema.json").read_bytes(),
        frozenset({7}),
    )


@pytest.fixture
def candidate(
    tmp_path: Path, deployment: bootstrap.BootstrapDeploymentPolicy
) -> tuple[Path, dict[str, object], dict[str, object]]:
    root = tmp_path / "candidate"
    sources = {
        "scripts/__init__.py": b"raise RuntimeError('do not execute')\n",
        "scripts/main.py": b"raise RuntimeError('do not execute')\n",
    }
    rows = []
    for name, raw in sorted(sources.items()):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        rows.append({"path": name, "size_bytes": len(raw), "sha256": digest(raw)})
    manifest = {
        "schema_version": 1,
        "kind": "fieldkit.release-controller-manifest",
        "entrypoint": "scripts/main.py",
        "members": rows,
    }
    selection = fixture_selection()
    mapping(selection["controller"]).update(
        entrypoint=manifest["entrypoint"], members=[{**row, "role": "descriptive-only"} for row in rows]
    )
    mapping(selection["policy"]).update(
        selection_schema_sha256=digest(deployment.selection_schema_bytes),
        receipt_schema_sha256=digest(deployment.receipt_schema_bytes),
    )
    return root, selection, manifest


def inputs(
    selection: dict[str, object], manifest: dict[str, object]
) -> tuple[bytes, bytes, bootstrap.ExternalSelectionAnchor]:
    raw_manifest = encoded(manifest)
    mapping(selection["controller"])["manifest_sha256"] = digest(raw_manifest)
    raw_selection = encoded(selection)
    anchor = bootstrap.ExternalSelectionAnchor(
        digest(raw_selection), "synthetic-test-pin", NOW - timedelta(hours=1), NOW + timedelta(hours=1), 7
    )
    return raw_selection, raw_manifest, anchor


def authenticate(
    candidate: tuple[Path, dict[str, object], dict[str, object]], deployment: bootstrap.BootstrapDeploymentPolicy
) -> bootstrap.SelectionBoundControllerCapture:
    root, selection, manifest = candidate
    selected, detached, anchor = inputs(selection, manifest)
    return bootstrap.authenticate_initial_export_selection(
        selected, detached, anchor=anchor, deployment=deployment, controller_root=root, now=NOW
    )


def test_exact_immutable_capture_has_no_approval(
    candidate: tuple[Path, dict[str, object], dict[str, object]], deployment: bootstrap.BootstrapDeploymentPolicy
) -> None:
    result = authenticate(candidate, deployment)
    assert isinstance(result, bootstrap.SelectionBoundControllerCapture)
    selected, detached, anchor = inputs(candidate[1], candidate[2])
    assert result.selection_bytes == selected
    assert result.controller_manifest_bytes == detached
    assert result.selection_sha256 == anchor.selection_sha256
    assert result.controller_manifest_sha256 == digest(detached)
    assert result.anchor_reference == "synthetic-test-pin"
    assert result.anchor_generation == 7
    original = result.controller.members
    for path, raw in original:
        assert (candidate[0] / path).read_bytes() == raw
        (candidate[0] / path).write_bytes(b"changed")
    assert result.controller.members == original
    assert not any(
        hasattr(result, name) for name in ("decision", "signature", "receipt", "runtime", "approval", "checks")
    )
    with pytest.raises(FrozenInstanceError, match="anchor_generation"):
        attribute = "anchor_generation"
        setattr(result, attribute, 8)


def test_retained_selection_uses_current_policy_without_reopening_sources(
    candidate: tuple[Path, dict[str, object], dict[str, object]], deployment: bootstrap.BootstrapDeploymentPolicy
) -> None:
    capture = authenticate(candidate, deployment)
    _, _, anchor = inputs(candidate[1], candidate[2])
    for path, _ in capture.controller.members:
        (candidate[0] / path).write_bytes(b"source no longer matches")
    result = bootstrap.validate_retained_selection(capture, anchor=anchor, deployment=deployment, now=NOW)
    assert result == json.loads(capture.selection_bytes)
    assert mapping(result["controller"])["entrypoint"] == capture.controller.entrypoint
    assert closure.validate_controller_capture(capture.controller) == capture.controller.members


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("selection-bytes", "raw selection digest"),
        ("manifest-bytes", "raw detached manifest digest"),
        ("selection-digest", "retained selection provenance"),
        ("manifest-digest", "retained selection provenance"),
        ("reference", "retained selection provenance"),
        ("generation", "retained selection provenance"),
        ("boolean-generation", "retained selection provenance"),
        ("entrypoint", "retained controller disagrees"),
        ("member-bytes", "retained controller disagrees"),
        ("member-missing", "retained controller disagrees"),
        ("member-duplicate", "duplicate member"),
    ],
)
def test_retained_selection_rejects_forged_capture(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    case: str,
    message: str,
) -> None:
    capture = authenticate(candidate, deployment)
    _, _, anchor = inputs(candidate[1], candidate[2])
    if case == "selection-bytes":
        capture = replace(capture, selection_bytes=capture.selection_bytes + b" ")
    elif case == "manifest-bytes":
        capture = replace(capture, controller_manifest_bytes=capture.controller_manifest_bytes + b" ")
    elif case == "selection-digest":
        capture = replace(capture, selection_sha256="0" * 64)
    elif case == "manifest-digest":
        capture = replace(capture, controller_manifest_sha256="0" * 64)
    elif case == "reference":
        capture = replace(capture, anchor_reference="another-synthetic-reference")
    elif case == "generation":
        capture = replace(capture, anchor_generation=8)
    elif case == "boolean-generation":
        capture = replace(capture, anchor_generation=True)
    elif case == "entrypoint":
        capture = replace(capture, controller=replace(capture.controller, entrypoint="scripts/__init__.py"))
    elif case == "member-bytes":
        path, raw = capture.controller.members[-1]
        changed = (*capture.controller.members[:-1], (path, b"x" * len(raw)))
        capture = replace(capture, controller=replace(capture.controller, members=changed))
    elif case == "member-missing":
        capture = replace(capture, controller=replace(capture.controller, members=capture.controller.members[1:]))
    elif case == "member-duplicate":
        duplicated = (*capture.controller.members, capture.controller.members[0])
        capture = replace(capture, controller=replace(capture.controller, members=duplicated))
    with pytest.raises(ValueError, match=message):
        bootstrap.validate_retained_selection(capture, anchor=anchor, deployment=deployment, now=NOW)


@pytest.mark.parametrize("case", ["expired", "revoked-generation", "schema-change", "changed-reference"])
def test_retained_selection_rechecks_current_authority_inputs(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    case: str,
) -> None:
    capture = authenticate(candidate, deployment)
    _, _, anchor = inputs(candidate[1], candidate[2])
    message = "validity interval"
    if case == "expired":
        anchor = replace(anchor, valid_before=NOW)
    elif case == "revoked-generation":
        deployment = replace(deployment, allowed_generations=frozenset())
        message = "generation is disallowed"
    elif case == "schema-change":
        deployment = replace(deployment, selection_schema_bytes=deployment.selection_schema_bytes + b" ")
        message = "schema digests disagree"
    elif case == "changed-reference":
        anchor = replace(anchor, approval_reference="another-synthetic-reference")
        message = "retained selection provenance"
    with pytest.raises(ValueError, match=message):
        bootstrap.validate_retained_selection(capture, anchor=anchor, deployment=deployment, now=NOW)


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong", "expired", "future", "revoked", "bool-generation", "naive", "claim", "empty-generations"],
)
def test_anchor_fails_before_parsing_or_capture(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root, selection, manifest = candidate
    selected, detached, anchor = inputs(selection, manifest)
    if case == "missing":
        invalid_anchor: object = None
    elif case == "wrong":
        invalid_anchor = replace(anchor, selection_sha256="f" * 64)
    elif case == "expired":
        invalid_anchor = replace(anchor, valid_before=NOW)
    elif case == "future":
        invalid_anchor = replace(anchor, valid_after=NOW + timedelta(seconds=1))
    elif case in {"revoked", "bool-generation"}:
        invalid_anchor = replace(anchor, generation=True if case == "bool-generation" else 8)
    elif case == "naive":
        invalid_anchor = replace(anchor, valid_after=NOW.replace(tzinfo=None))
    elif case == "claim":
        invalid_anchor = {"selection_sha256": digest(selected), "approved": True}
    else:
        invalid_anchor = anchor
        deployment = replace(deployment, allowed_generations=frozenset())

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must reject anchor before parsing or acquisition")

    monkeypatch.setattr(bootstrap, "_validate_selection", forbidden)
    monkeypatch.setattr(bootstrap, "capture_controller_closure", forbidden)
    with pytest.raises(ValueError, match="anchor"):
        bootstrap.authenticate_initial_export_selection(
            selected,
            detached,
            anchor=invalid_anchor,  # type: ignore[arg-type]
            deployment=deployment,
            controller_root=root,
            now=NOW,
        )


@pytest.mark.parametrize("target", ["selection", "manifest"])
def test_equivalent_json_raw_bytes_fail(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    target: str,
) -> None:
    root, selection, manifest = candidate
    selected, detached, anchor = inputs(selection, manifest)
    if target == "selection":
        selected += b"\n"
    else:
        detached += b"\n"
    with pytest.raises(ValueError, match=r"raw.*digest"):
        bootstrap.authenticate_initial_export_selection(
            selected, detached, anchor=anchor, deployment=deployment, controller_root=root, now=NOW
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"duplicate":1,"duplicate":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
        b"[]",
        b"\xff",
        b" " * (bootstrap.MAX_BOOTSTRAP_JSON_BYTES + 1),
    ],
)
@pytest.mark.parametrize("target", ["selection", "manifest"])
def test_strict_json(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    raw: bytes,
    target: str,
) -> None:
    root, selection, manifest = candidate
    selected, detached, anchor = inputs(selection, manifest)
    if target == "selection":
        selected = raw
    else:
        detached = raw
        mapping(selection["controller"])["manifest_sha256"] = digest(raw)
        selected = encoded(selection)
    anchor = replace(anchor, selection_sha256=digest(selected))
    with pytest.raises(ValueError, match=r"JSON|object|bounded|structure"):
        bootstrap.authenticate_initial_export_selection(
            selected, detached, anchor=anchor, deployment=deployment, controller_root=root, now=NOW
        )


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "duplicate",
        "unsorted",
        "noncanonical",
        "oversize",
        "too-many",
        "missing-init",
        "entrypoint",
        "selection-mismatch",
        "schema-hash",
        "role-misuse",
    ],
)
def test_manifest_and_selection_disagreement(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    case: str,
) -> None:
    root, selection, manifest = candidate
    rows = manifest["members"]
    assert isinstance(rows, list)
    if case == "unknown":
        manifest["approved"] = True
    elif case == "duplicate":
        rows.append(rows[0])
    elif case == "unsorted":
        rows.reverse()
    elif case == "noncanonical":
        mapping(rows[0])["path"] = "scripts/../escape.py"
    elif case == "oversize":
        mapping(rows[0])["size_bytes"] = closure.MAX_CONTROLLER_BYTES + 1
    elif case == "too-many":
        rows.extend([rows[0]] * closure.MAX_CONTROLLER_MEMBERS)
    elif case == "missing-init":
        rows.pop(0)
        (root / "scripts/__init__.py").unlink()
    elif case == "entrypoint":
        manifest["entrypoint"] = "scripts/missing.py"
    elif case == "schema-hash":
        mapping(selection["policy"])["selection_schema_sha256"] = "f" * 64
    if case not in {"selection-mismatch", "schema-hash"}:
        mapping(selection["controller"]).update(
            entrypoint=manifest["entrypoint"],
            members=[{**mapping(row), "role": "allows-application-imports"} for row in rows],
        )
    if case == "selection-mismatch":
        mapping(selection["controller"])["entrypoint"] = "scripts/other.py"
    if case == "role-misuse":
        raw = b"import fieldkit\n"
        (root / "scripts/main.py").write_bytes(raw)
        mapping(rows[1]).update(size_bytes=len(raw), sha256=digest(raw))
        mapping(selection["controller"])["members"] = [
            {**mapping(row), "role": "allows-application-imports"} for row in rows
        ]
    with pytest.raises(
        ValueError, match=r"manifest|selection|controller|initializer|entrypoint|application|structure|canonical"
    ):
        authenticate(candidate, deployment)


@pytest.mark.parametrize(
    "source",
    [b"import fieldkit\n", b"from fieldkit.cli import main\n", b"import src.fieldkit\n", b"from src import fieldkit\n"],
)
def test_application_direct_imports_rejected(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    source: bytes,
) -> None:
    root, selection, manifest = candidate
    rows = manifest["members"]
    assert isinstance(rows, list)
    (root / "scripts/main.py").write_bytes(source)
    mapping(rows[1]).update(size_bytes=len(source), sha256=digest(source))
    mapping(selection["controller"])["members"] = [{**mapping(row), "role": "descriptive"} for row in rows]
    with pytest.raises(ValueError, match="application imports"):
        authenticate(candidate, deployment)


@pytest.mark.parametrize("case", ["extra", "symlink", "mutation"])
def test_capture_failure_closes_descriptors(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root, _, _ = candidate
    target = root / "scripts/main.py"
    if case == "extra":
        (root / "extra").write_bytes(b"extra")
    elif case == "symlink":
        target.unlink()
        target.symlink_to(root / "scripts/__init__.py")
    else:
        read = release_filesystem.read_regular_file

        def mutate(descriptor: int, *, maximum_bytes: int) -> bytes:
            data = read(descriptor, maximum_bytes=maximum_bytes)
            if os.fstat(descriptor).st_ino == target.stat().st_ino:
                target.write_bytes(b"changed during read")
            return data

        monkeypatch.setattr(release_filesystem, "read_regular_file", mutate)
    opened: list[int] = []
    original_open = os.open

    def record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", record_open)
    with pytest.raises((ValueError, OSError), match=r"unexpected|regular|changed|symbolic"):
        authenticate(candidate, deployment)
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_unregistered_schema_reference_rejected(
    candidate: tuple[Path, dict[str, object], dict[str, object]], deployment: bootstrap.BootstrapDeploymentPolicy
) -> None:
    schema = mapping(json.loads(deployment.selection_schema_bytes))
    schema["unused"] = {"$ref": "https://example.com/forbidden.json"}
    changed = replace(deployment, selection_schema_bytes=encoded(schema))
    mapping(candidate[1]["policy"])["selection_schema_sha256"] = digest(changed.selection_schema_bytes)
    with pytest.raises(ValueError, match="unregistered reference"):
        authenticate(candidate, changed)


def test_isolated_deployed_bundle_never_imports_candidate(
    tmp_path: Path,
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
) -> None:
    root, selection, manifest = candidate
    marker = tmp_path / "executed"
    hostile = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n".encode()
    rows = manifest["members"]
    assert isinstance(rows, list)
    for row in rows:
        member = mapping(row)
        (root / str(member["path"])).write_bytes(hostile)
        member.update(size_bytes=len(hostile), sha256=digest(hostile))
    mapping(selection["controller"])["members"] = [{**mapping(row), "role": "entrypoint"} for row in rows]
    selected, detached, _ = inputs(selection, manifest)
    bundle = tmp_path / "trusted"
    package = bundle / "scripts"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    for name in (
        "release_controller_bootstrap.py",
        "release_controller_closure.py",
        "release_filesystem.py",
        "json_policy.py",
    ):
        (package / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    for name, data in (
        ("selection", selected),
        ("manifest", detached),
        ("selection-schema", deployment.selection_schema_bytes),
        ("receipt-schema", deployment.receipt_schema_bytes),
    ):
        (bundle / name).write_bytes(data)
    ambient = tmp_path / "ambient"
    (ambient / "scripts").mkdir(parents=True)
    (ambient / "scripts/__init__.py").write_bytes(hostile)
    (ambient / "sitecustomize.py").write_bytes(hostile)
    (ambient / "jsonschema.py").write_bytes(hostile)
    code = """
import sys
from pathlib import Path
from datetime import datetime, UTC, timedelta
import hashlib
bundle, candidate = map(Path, sys.argv[1:])
sys.path.insert(0, str(bundle))
from scripts.release_controller_bootstrap import authenticate_initial_export_selection, BootstrapDeploymentPolicy, ExternalSelectionAnchor
selection = (bundle / 'selection').read_bytes()
now = datetime(2026, 9, 30, tzinfo=UTC)
result = authenticate_initial_export_selection(selection, (bundle / 'manifest').read_bytes(), anchor=ExternalSelectionAnchor(hashlib.sha256(selection).hexdigest(), 'synthetic', now-timedelta(hours=1), now+timedelta(hours=1), 7), deployment=BootstrapDeploymentPolicy((bundle/'selection-schema').read_bytes(), (bundle/'receipt-schema').read_bytes(), frozenset({7})), controller_root=candidate, now=now)
assert result.selection_bytes == selection
assert 'fieldkit' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(bundle), str(root)],
        cwd=ambient,
        env={"PYTHONPATH": str(ambient), "PYTHONHOME": str(ambient), "PATH": os.defpath},
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert not marker.exists()


@pytest.mark.parametrize("size", [37.0, True])
def test_selected_sizes_are_exact_integers(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    size: object,
) -> None:
    rows = mapping(candidate[1]["controller"])["members"]
    assert isinstance(rows, list)
    mapping(rows[0])["size_bytes"] = size
    with pytest.raises(ValueError, match=r"structure|exact integers"):
        authenticate(candidate, deployment)


@pytest.mark.parametrize("path", ["fieldkit.py", "src/fieldkit.py", "fieldkit/module.py", "src/fieldkit/module.py"])
def test_application_member_paths_rejected(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    path: str,
) -> None:
    root, selection, manifest = candidate
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"")
    rows = manifest["members"]
    assert isinstance(rows, list)
    rows.append({"path": path, "size_bytes": 0, "sha256": digest(b"")})
    rows.sort(key=lambda row: str(mapping(row)["path"]))
    mapping(selection["controller"])["members"] = [{**mapping(row), "role": "permitted-app"} for row in rows]
    with pytest.raises(ValueError, match="application members"):
        authenticate(candidate, deployment)


@pytest.mark.parametrize(
    "references",
    [
        {"$ref": "#/$defs/sha256", "$dynamicRef": "https://example.com/unregistered.json"},
        {"$ref": ""},
        {"$dynamicRef": None},
    ],
)
def test_all_schema_reference_keywords_are_closed(
    candidate: tuple[Path, dict[str, object], dict[str, object]],
    deployment: bootstrap.BootstrapDeploymentPolicy,
    references: dict[str, object],
) -> None:
    schema = mapping(json.loads(deployment.selection_schema_bytes))
    schema["unused"] = references
    changed = replace(deployment, selection_schema_bytes=encoded(schema))
    mapping(candidate[1]["policy"])["selection_schema_sha256"] = digest(changed.selection_schema_bytes)
    with pytest.raises(ValueError, match="unregistered reference"):
        authenticate(candidate, changed)
