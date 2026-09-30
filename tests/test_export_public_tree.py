"""Tests for deterministic, fail-closed clean public-tree export."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot
from scripts import export_public_tree, quality_source, release_approval_archive

pytestmark = pytest.mark.unit


def test_git_output_is_bounded_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, revision, policy = _repository(tmp_path)
    oid = _git(repo, "rev-parse", f"{revision}:{policy.relative_to(repo).as_posix()}")
    monkeypatch.setattr(quality_source, "GIT_OUTPUT_LIMIT_BYTES", 80)
    with pytest.raises(export_public_tree.ExportError, match=r"bounded|output|overflow"):
        export_public_tree._git(repo, "cat-file", "blob", oid)


def test_inventory_count_precedes_record_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quality_source, "MAX_ENTRY_COUNT", 2)
    monkeypatch.setattr(export_public_tree, "_git", lambda *args: b"malformed\0malformed\0malformed\0")
    with pytest.raises(export_public_tree.ExportError, match=r"inventory.*bound"):
        export_public_tree._source_entries(tmp_path, "a" * 40)


def test_policy_loading_refuses_leaf_symlink(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "policy.json")
    linked = tmp_path / "linked-policy.json"
    linked.symlink_to(policy)
    with pytest.raises(export_public_tree.ExportError, match=r"stable regular|cannot load"):
        export_public_tree._load_policy(linked)


def test_working_policy_is_bounded_before_json_parse(tmp_path: Path) -> None:
    policy = tmp_path / "oversized-policy.json"
    policy.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(export_public_tree.ExportError, match=r"stable regular|size bound"):
        export_public_tree._load_policy(policy)


@pytest.mark.parametrize("probe", [False, True])
def test_fd_member_size_is_checked_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: bool,
) -> None:
    (tmp_path / "large").write_bytes(b"x" * 81)
    monkeypatch.setattr(quality_source, "GIT_OUTPUT_LIMIT_BYTES", 80)
    reader = MagicMock()
    reader.read.side_effect = AssertionError("oversized member reached read")

    def fdopen(descriptor: int, mode: str) -> MagicMock:
        reader.fileno.return_value = descriptor
        reader.__enter__.return_value = reader
        reader.__exit__.side_effect = lambda *args: os.close(descriptor)
        return reader

    if probe:
        monkeypatch.setattr(os, "fdopen", fdopen)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(export_public_tree.ExportError, match="byte bound"):
            export_public_tree._read_file_at(directory_fd, "large")
    finally:
        os.close(directory_fd)
    reader.read.assert_not_called()


def test_materialized_member_cannot_exceed_read_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest = export_public_tree.export_tree(repo, revision, policy, destination, tmp_path / "manifest.json")
    assert manifest.source_commit == revision
    (destination / "README.md").write_bytes(b"x" * 81)
    monkeypatch.setattr(quality_source, "GIT_OUTPUT_LIMIT_BYTES", 80)
    with pytest.raises(export_public_tree.ExportError, match="byte bound"):
        export_public_tree._materialized_entries(destination, manifest)


def test_committed_policy_parses_only_captured_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, revision, policy_path = _repository(tmp_path)
    expected = export_public_tree._load_policy(policy_path)
    captured = 0

    def capture(path: Path, *, max_bytes: int) -> TextSnapshot:
        nonlocal captured
        result = read_text_snapshot(path, max_bytes=max_bytes)
        if path == policy_path:
            captured += 1
            policy = json.loads(result.content)
            policy["expected_repository"] = "example/substituted"
            path.write_text(json.dumps(policy), encoding="utf-8")
        return result

    monkeypatch.setattr(export_public_tree, "read_text_snapshot", capture)
    result = export_public_tree._load_committed_policy(repo, revision, policy_path)
    assert result[0] == expected
    assert captured == 1


@pytest.mark.parametrize("mutation", ["bytes", "replacement", "ancestor"])
def test_export_refuses_manifest_change_during_final_tree_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    manifest_parent = tmp_path / "manifests"
    manifest_parent.mkdir()
    manifest_path = manifest_parent / "manifest.json"
    verify_tree = export_public_tree._verify_tree_at
    calls = 0

    def change_manifest(root_fd: int, entries: tuple[export_public_tree.TreeEntry, ...]) -> None:
        nonlocal calls
        calls += 1
        verify_tree(root_fd, entries)
        if calls == 3:
            if mutation == "bytes":
                manifest_path.write_bytes(b"changed")
            elif mutation == "replacement":
                content = manifest_path.read_bytes()
                manifest_path.unlink()
                manifest_path.write_bytes(content)
            else:
                manifest_parent.rename(tmp_path / "retained-manifests")
                manifest_parent.mkdir()
                manifest_path.write_bytes(b"sentinel")

    monkeypatch.setattr(export_public_tree, "_verify_tree_at", change_manifest)
    descriptors_before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(export_public_tree.ExportError, match=r"manifest|parent directory identity"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", manifest_path)
    assert calls == 3
    assert (tmp_path / "public" / "README.md").read_bytes() == b"public\n"
    assert manifest_path.exists()
    assert set(Path("/proc/self/fd").iterdir()) == descriptors_before


def test_export_staging_substitution_preserves_unrelated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "README.md").write_bytes(b"sentinel")
    real_git = export_public_tree._git
    substituted = False

    def substitute_staging(repository: Path, *args: str) -> bytes:
        nonlocal substituted
        if args[:2] == ("cat-file", "blob") and not substituted:
            stages = list(tmp_path.glob(".public-*"))
            if stages:
                stage = stages[0]
                stage.rename(tmp_path / "retained-stage")
                stage.symlink_to(protected, target_is_directory=True)
                substituted = True
        return real_git(repository, *args)

    monkeypatch.setattr(export_public_tree, "_git", substitute_staging)
    descriptors_before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(export_public_tree.ExportError, match=r"identity|redirected|substitut"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert substituted
    assert (protected / "README.md").read_bytes() == b"sentinel"
    assert set(protected.iterdir()) == {protected / "README.md"}
    assert not (tmp_path / "manifest.json").exists()
    assert set(Path("/proc/self/fd").iterdir()) == descriptors_before


@pytest.mark.parametrize("occupied", ["public", "manifest.json"])
def test_export_rejects_dangling_output_symlink(tmp_path: Path, occupied: str) -> None:
    repo, revision, policy = _repository(tmp_path)
    path = tmp_path / occupied
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(export_public_tree.ExportError, match="must not exist"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert path.is_symlink()
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("occupied", ["public", "manifest.json"])
@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_export_concurrent_occupied_output_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied: str,
    kind: str,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    real_git = export_public_tree._git
    path = tmp_path / occupied
    planted = False

    def occupy_output(repository: Path, *args: str) -> bytes:
        nonlocal planted
        if args[:2] == ("cat-file", "blob") and list(tmp_path.glob(".public-*")) and not planted:
            if kind == "file":
                path.write_bytes(b"sentinel")
            elif kind == "directory":
                path.mkdir()
                (path / "sentinel").write_bytes(b"sentinel")
            else:
                path.symlink_to(tmp_path / "missing")
            planted = True
        return real_git(repository, *args)

    monkeypatch.setattr(export_public_tree, "_git", occupy_output)
    with pytest.raises(export_public_tree.ExportError, match="must not already exist"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert planted
    if kind == "file":
        assert path.read_bytes() == b"sentinel"
    elif kind == "directory":
        assert (path / "sentinel").read_bytes() == b"sentinel"
    else:
        assert path.is_symlink()
        assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("parent", ["export-parent", "manifest-parent"])
def test_export_parent_substitution_does_not_redirect_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: str,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    export_parent = tmp_path / "export-parent"
    manifest_parent = tmp_path / "manifest-parent"
    export_parent.mkdir()
    manifest_parent.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "sentinel").write_bytes(b"sentinel")
    real_git = export_public_tree._git
    substituted = False

    def redirect_parent(repository: Path, *args: str) -> bytes:
        nonlocal substituted
        if args[:2] == ("cat-file", "blob") and list(export_parent.glob(".public-*")) and not substituted:
            original = tmp_path / parent
            original.rename(tmp_path / "retained-parent")
            original.symlink_to(protected, target_is_directory=True)
            substituted = True
        return real_git(repository, *args)

    monkeypatch.setattr(export_public_tree, "_git", redirect_parent)
    descriptors_before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(export_public_tree.ExportError, match=r"real path components|identity"):
        export_public_tree.export_tree(
            repo, revision, policy, export_parent / "public", manifest_parent / "manifest.json"
        )
    assert substituted
    assert set(protected.iterdir()) == {protected / "sentinel"}
    assert (protected / "sentinel").read_bytes() == b"sentinel"
    assert set(Path("/proc/self/fd").iterdir()) == descriptors_before


def test_export_member_substitution_is_refused_without_following_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "sentinel").write_bytes(b"sentinel")
    real_git = export_public_tree._git

    def redirect_member(repository: Path, *args: str) -> bytes:
        stages = list(tmp_path.glob(".public-*"))
        if args[:2] == ("cat-file", "blob") and stages and not (stages[0] / "docs").exists():
            (stages[0] / "docs").symlink_to(protected, target_is_directory=True)
        return real_git(repository, *args)

    monkeypatch.setattr(export_public_tree, "_git", redirect_member)
    with pytest.raises(export_public_tree.ExportError, match="publication failed"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert set(protected.iterdir()) == {protected / "sentinel"}
    assert (protected / "sentinel").read_bytes() == b"sentinel"


def test_export_refuses_post_publish_byte_substitution_and_retains_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    write_manifest = export_public_tree._write_manifest

    def modify_published(
        path: Path, manifest: export_public_tree.ExportManifest, *, parent_fd: int
    ) -> tuple[int, bytes]:
        binding = write_manifest(path, manifest, parent_fd=parent_fd)
        (destination / "README.md").write_bytes(b"changed")
        return binding

    monkeypatch.setattr(export_public_tree, "_write_manifest", modify_published)
    with pytest.raises(export_public_tree.ExportError, match="bytes or mode changed"):
        export_public_tree.export_tree(repo, revision, policy, destination, tmp_path / "manifest.json")
    assert (destination / "README.md").read_bytes() == b"changed"
    assert (tmp_path / "manifest.json").is_file()


@pytest.mark.parametrize("substitution", ["directory", "symlink", "bytes"])
def test_export_refuses_substitution_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "README.md").write_bytes(b"sentinel")
    rename = release_approval_archive._rename_no_replace_at

    def substitute_then_rename(parent_fd: int, source: str, destination: str) -> None:
        stage = tmp_path / source
        if destination == "public":
            if substitution == "bytes":
                (stage / "README.md").write_bytes(b"changed")
            else:
                stage.rename(tmp_path / "retained-stage")
                if substitution == "directory":
                    stage.mkdir()
                    (stage / "README.md").write_bytes(b"replacement")
                else:
                    stage.symlink_to(protected, target_is_directory=True)
        rename(parent_fd, source, destination)

    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", substitute_then_rename)
    descriptors_before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(export_public_tree.ExportError, match=r"identity|bytes or mode"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert (protected / "README.md").read_bytes() == b"sentinel"
    assert not (tmp_path / "manifest.json").exists()
    assert (tmp_path / "public").exists()
    assert set(Path("/proc/self/fd").iterdir()) == descriptors_before


def test_export_manifest_temporary_substitution_preserves_unrelated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, revision, policy = _repository(tmp_path)
    protected = tmp_path / "protected.json"
    protected.write_bytes(b"sentinel")
    rename = release_approval_archive._rename_no_replace_at

    def substitute_manifest(parent_fd: int, source: str, destination: str) -> None:
        if source.startswith(".manifest.json-"):
            path = tmp_path / source
            path.rename(tmp_path / "retained-manifest.json")
            path.symlink_to(protected)
        rename(parent_fd, source, destination)

    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", substitute_manifest)
    with pytest.raises(export_public_tree.ExportError, match="publication failed"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert protected.read_bytes() == b"sentinel"
    assert (tmp_path / "public" / "README.md").read_bytes() == b"public\n"
    assert (tmp_path / "manifest.json").is_symlink()


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_companion_exact_permissions.py",
        "tests/test_companion_feed_permissions.py",
        "tests/test_companion_quota_permissions.py",
        "tests/test_meeting_base_profile.py",
        "tests/test_saved_report_viewers.py",
    ],
)
def test_cleanup_regressions_have_exact_public_export_ownership(path: str) -> None:
    policy = export_public_tree._load_policy(
        Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json"
    )
    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)
    assert len(included) == 1
    assert included[0].rule_id == "include-tests"
    assert included[0].category == "test"
    assert excluded == ()


@pytest.mark.parametrize("module", ["first_user_guides_contract", "sf_auth_guide_contract"])
def test_pending_guide_modules_have_exact_public_export_ownership(module: str) -> None:
    policy = export_public_tree._load_policy(
        Path(__file__).parents[1] / "docs/release-readiness/public-tree-policy.json"
    )
    path = f"tests/test_{module}.py"
    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)
    assert [entry.path for entry in included] == [path]
    assert included[0].rule_id == "include-tests"
    assert excluded == ()
    adjacent = f"tests/test_{module}_unreviewed.py"
    with pytest.raises(export_public_tree.ExportError, match="unclassified tracked path"):
        export_public_tree._classify(((adjacent, "100644", "a" * 40),), policy)


@pytest.mark.parametrize(
    "path",
    [
        "docs/autonomy-roadmap.md",
        "docs/true-up-ledger-2026-07-18.md",
        "docs/true-up-ledger-2026-07-28.md",
    ],
)
def test_private_execution_records_are_not_public_pages(path: str) -> None:
    root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(root / "docs/release-readiness/public-tree-policy.json")
    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)

    assert included == ()
    assert len(excluded) == 1
    assert excluded[0].category == "private_history"
    surface = json.loads((root / "docs/release-readiness/public-surface-policy.json").read_text(encoding="utf-8"))
    assert path in surface["categories"]["private_history_excluded"]
    site = yaml.safe_load((root / "mkdocs.yml").read_text(encoding="utf-8"))
    relative = path.removeprefix("docs/")
    assert relative in site["exclude_docs"].splitlines()
    assert relative not in json.dumps(site["nav"])


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, Path]:
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    (repo / "private.txt").write_text("private\n", encoding="utf-8")
    policy = _policy(repo / "docs/release-readiness/public-tree-policy.json")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD"), policy


def _policy(path: Path, *, public_patterns: list[str] | None = None) -> Path:
    policy = {
        "schema_version": 1,
        "expected_repository": "example/fieldkit-cli",
        "planned_tag": "v1.0.0",
        "rules": [
            {
                "id": "product",
                "action": "include",
                "category": "product",
                "patterns": [
                    *(public_patterns or ["README.md"]),
                    "docs/release-readiness/public-tree-policy.json",
                ],
                "rationale": "Public product files.",
            },
            {
                "id": "private",
                "action": "exclude",
                "category": "private_history",
                "patterns": ["private.txt"],
                "rationale": "Private test material.",
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_export_uses_committed_objects_and_emits_external_manifest(tmp_path: Path) -> None:
    """Dirty working-tree bytes cannot enter an export of an explicit commit."""
    repo, revision, policy = _repository(tmp_path)
    (repo / "README.md").write_text("dirty private change\n", encoding="utf-8")
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"

    manifest = export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)

    assert (destination / "README.md").read_text(encoding="utf-8") == "public\n"
    assert not (destination / "private.txt").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "manifest.json").exists()
    assert manifest.source_commit == revision
    assert manifest.expected_repository == "example/fieldkit-cli"
    assert manifest.planned_tag == "v1.0.0"
    assert export_public_tree.verify_export(repo, destination, manifest_path, policy) == manifest


def test_export_rejects_unclassified_path_before_creating_destination(tmp_path: Path) -> None:
    """A newly tracked path cannot leak through a permissive default."""
    repo, revision, policy = _repository(tmp_path)
    (repo / "unknown.txt").write_text("unknown\n", encoding="utf-8")
    _git(repo, "add", "unknown.txt")
    _git(repo, "commit", "-qm", "add unknown")
    revision = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "public"

    with pytest.raises(export_public_tree.ExportError, match=r"unclassified.*unknown\.txt"):
        export_public_tree.export_tree(repo, revision, policy, destination, tmp_path / "manifest.json")

    assert not destination.exists()


def test_export_excludes_valid_unreleased_changelog_fragments(tmp_path: Path) -> None:
    """New valid fragments stay out of the public tree until release assembly."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / "README.md").write_text("format\n", encoding="utf-8")
    (repo / "changelog.d" / "add-a-public-release-note.md").write_text("### Add a note\n", encoding="utf-8")
    policy_path = _policy(repo / "docs/release-readiness/public-tree-policy.json")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["rules"][0]["patterns"].append("changelog.d/README.md")
    policy["rules"].append(
        {
            "id": "pre-release-fragments",
            "action": "exclude",
            "category": "pre_release_history",
            "patterns": ["changelog.d/[a-z0-9]*.md"],
            "rationale": "Release assembly consumes valid fragments.",
        }
    )
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    revision = _git(repo, "rev-parse", "HEAD")

    manifest = export_public_tree.export_tree(
        repo, revision, policy_path, tmp_path / "public", tmp_path / "manifest.json"
    )

    assert (tmp_path / "public" / "changelog.d" / "README.md").is_file()
    assert (tmp_path / "public" / "changelog.d" / "add-a-public-release-note.md").exists() is False
    assert manifest.excluded[0].rule_id == "pre-release-fragments"


def test_public_release_contracts_are_classified_for_clean_export() -> None:
    """Release governance and bundle contracts remain available to public consumers."""
    repo_root = Path(__file__).parents[1]
    tree_policy = json.loads((repo_root / "docs/release-readiness/public-tree-policy.json").read_text(encoding="utf-8"))
    surface_policy = json.loads(
        (repo_root / "docs/release-readiness/public-surface-policy.json").read_text(encoding="utf-8")
    )
    tree_contracts = next(rule for rule in tree_policy["rules"] if rule["id"] == "include-public-documentation")
    public_repository_only = surface_policy["categories"]["public_repository_only"]
    contracts = {
        "docs/release-readiness/release-bundle-provenance.schema.json",
        "docs/release-readiness/release-consumer-evidence.schema.json",
        "docs/release-readiness/release-governance-policy.json",
        "docs/release-readiness/release-governance-policy.schema.json",
        "docs/release-readiness/release-policy.json",
    }

    assert contracts <= set(tree_contracts["patterns"])
    assert contracts <= set(public_repository_only)


def test_typing_shadow_inventory_is_explicitly_owned() -> None:
    """Only the reviewed third-party stub may enter the type checker's shadow path."""
    root = Path(__file__).parents[1]
    expected = {"typings/google_auth_httplib2/__init__.pyi"}
    observed = {path.relative_to(root).as_posix() for path in (root / "typings").rglob("*") if path.is_file()}
    assert observed == expected
    policy = export_public_tree._load_policy(root / "docs/release-readiness/public-tree-policy.json")
    included, excluded = export_public_tree._classify(
        tuple((path, "100644", "a" * 40) for path in sorted(expected)), policy
    )
    assert {entry.path for entry in included} == expected
    assert all(entry.category == "build" for entry in included)
    assert excluded == ()
    with pytest.raises(export_public_tree.ExportError, match="unclassified"):
        export_public_tree._classify((("typings/unreviewed.pyi", "100644", "a" * 40),), policy)


@pytest.mark.parametrize("path", [".skillsaw-baseline.json", ".opencode/check_skillsaw.py"])
def test_private_agent_baseline_is_not_exported(path: str) -> None:
    """Public quality gates must not inherit exceptions for private agent files."""
    repo_root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(repo_root / "docs/release-readiness/public-tree-policy.json")
    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)

    assert included == ()
    assert len(excluded) == 1
    assert excluded[0].category == "local_tool_state"


@pytest.mark.parametrize(
    "path",
    ["docs/adr/0011-require-meeting-task-writeback.md", "docs/adr/0012-journal-prepared-ingest-output.md"],
)
def test_private_ingest_decision_history_stays_private(path: str) -> None:
    """The internal decision record is excluded from export, inventory, and rendering."""
    repo_root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(repo_root / "docs/release-readiness/public-tree-policy.json")
    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)
    assert included == ()
    assert len(excluded) == 1
    assert excluded[0].category == "private_history"
    surface = json.loads((repo_root / "docs/release-readiness/public-surface-policy.json").read_text(encoding="utf-8"))
    assert path in surface["categories"]["private_history_excluded"]
    site = yaml.safe_load((repo_root / "mkdocs.yml").read_text(encoding="utf-8"))
    assert path.removeprefix("docs/") in site["exclude_docs"].splitlines()


@pytest.mark.parametrize(
    "path",
    [
        "scripts/oc_event_watcher.py",
        ".devcontainer/devcontainer.json",
        "scripts/check_work_order_done_checks.py",
        "tests/test_check_work_order_done_checks.py",
    ],
)
def test_private_operator_material_is_not_exported(path: str) -> None:
    """Private launchers and work-order validation are not public contributor tooling."""
    repo_root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(repo_root / "docs/release-readiness/public-tree-policy.json")

    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)

    assert included == ()
    assert len(excluded) == 1
    assert excluded[0].category == "private_operations"


@pytest.mark.parametrize(
    "path",
    ["scripts/new_helper.py", "scripts/skill_integrity/new_helper.py", "tests/test_new.py", "tests/fixtures/new.json"],
)
def test_new_infrastructure_requires_explicit_export_classification(path: str) -> None:
    """Adding a helper cannot implicitly approve its public distribution."""
    repo_root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(repo_root / "docs/release-readiness/public-tree-policy.json")

    with pytest.raises(export_public_tree.ExportError, match="unclassified tracked path"):
        export_public_tree._classify(((path, "100644", "a" * 40),), policy)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_privacy_guide_contract.py",
        "tests/test_gmail_guide_contract.py",
        "tests/test_gmail_refresh_input_selection.py",
    ],
)
def test_guide_contract_export_is_exact_not_a_wildcard(path: str) -> None:
    root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(root / "docs/release-readiness/public-tree-policy.json")

    included, excluded = export_public_tree._classify(((path, "100644", "a" * 40),), policy)

    assert [entry.path for entry in included] == [path]
    assert included[0].category == "test"
    assert excluded == ()
    with pytest.raises(export_public_tree.ExportError, match="unclassified tracked path"):
        export_public_tree._classify(((path.removesuffix(".py") + "_unreviewed.py", "100644", "a" * 40),), policy)


@pytest.mark.parametrize("namespace", ["scripts", "tests"])
def test_infrastructure_policy_is_literal_and_matches_tracked_inventory(namespace: str) -> None:
    """Reject wildcard approval and stale includes; excluded paths may be absent in exports."""
    repo_root = Path(__file__).parents[1]
    policy = export_public_tree._load_policy(repo_root / "docs/release-readiness/public-tree-policy.json")
    included: set[str] = set()
    excluded: set[str] = set()
    for rule in policy.rules:
        for pattern in rule.patterns:
            if pattern.startswith(f"{namespace}/"):
                assert not any(token in pattern for token in ("*", "?", "[")), pattern
                (included if rule.action == "include" else excluded).add(pattern)

    tracked = set(_git(repo_root, "ls-files", "-z", "--", f"{namespace}/").split("\0")) - {""}

    assert included == tracked - excluded
    assert included.isdisjoint(excluded)


def test_export_rejects_ambiguous_rules(tmp_path: Path) -> None:
    """Rule order cannot decide whether a tracked path is public."""
    repo, revision, policy_path = _repository(tmp_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["rules"].append(
        {
            "id": "duplicate-readme",
            "action": "include",
            "category": "documentation",
            "patterns": ["*.md"],
            "rationale": "Deliberate overlap for the test.",
        }
    )
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    _git(repo, "add", policy_path.relative_to(repo).as_posix())
    _git(repo, "commit", "-qm", "add overlapping policy rule")
    revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(export_public_tree.ExportError, match=r"ambiguous.*README\.md"):
        export_public_tree.export_tree(repo, revision, policy_path, tmp_path / "public", tmp_path / "manifest.json")


def test_export_rejects_existing_destination(tmp_path: Path) -> None:
    """Export never merges with or overwrites pre-existing state."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    destination.mkdir()

    with pytest.raises(export_public_tree.ExportError, match="destination must not exist"):
        export_public_tree.export_tree(repo, revision, policy, destination, tmp_path / "manifest.json")


@pytest.mark.parametrize(
    "key", ["schema_version", "synthetic-secret\x1b[31m\n", "x" * 8192], ids=["schema", "control", "large"]
)
def test_export_rejects_duplicate_policy_keys(tmp_path: Path, key: str) -> None:
    """Duplicate JSON keys cannot silently replace reviewed policy values."""
    repo, _, policy = _repository(tmp_path)
    encoded = json.dumps(key)
    policy.write_text(f'{{{encoded}:1,{encoded}:1,"expected_repository":"example/fieldkit-cli"}}', encoding="utf-8")
    _git(repo, "add", policy.relative_to(repo).as_posix())
    _git(repo, "commit", "-qm", "commit malformed policy")
    revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(export_public_tree.ExportError, match="invalid JSON input") as caught:
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")
    assert "invalid JSON input" in str(caught.value)
    assert key not in str(caught.value)


def test_export_rejects_dirty_policy(tmp_path: Path) -> None:
    """Classification uses only the policy committed in the named candidate."""
    repo, revision, policy = _repository(tmp_path)
    policy.write_text(policy.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(export_public_tree.ExportError, match="policy differs from committed candidate"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")


def test_export_rejects_external_policy(tmp_path: Path) -> None:
    """An untracked policy cannot govern a versioned release candidate."""
    repo, revision, policy = _repository(tmp_path)
    external_policy = tmp_path / "external-policy.json"
    external_policy.write_bytes(policy.read_bytes())

    with pytest.raises(export_public_tree.ExportError, match="policy must be a file tracked inside"):
        export_public_tree.export_tree(repo, revision, external_policy, tmp_path / "public", tmp_path / "manifest.json")


def test_export_rejects_alternate_tracked_policy_path(tmp_path: Path) -> None:
    """The CLI cannot emit a manifest that its own schema will reject."""
    repo, _, policy = _repository(tmp_path)
    alternate_policy = repo / "alternate-policy.json"
    alternate_policy.write_bytes(policy.read_bytes())
    _git(repo, "add", "alternate-policy.json")
    _git(repo, "commit", "-qm", "add alternate policy")
    revision = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "public"

    with pytest.raises(export_public_tree.ExportError, match="policy must use the canonical path"):
        export_public_tree.export_tree(repo, revision, alternate_policy, destination, tmp_path / "manifest.json")

    assert not destination.exists()


def test_verify_rejects_changed_export_bytes(tmp_path: Path) -> None:
    """A producer-authored manifest cannot conceal post-export tampering."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    (destination / "README.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(export_public_tree.ExportError, match=r"object mismatch.*README\.md"):
        export_public_tree.verify_export(repo, destination, manifest_path, policy)


def test_verify_rejects_changed_executable_mode(tmp_path: Path) -> None:
    """Git mode changes alter the independently reconstructed tree identity."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    (destination / "README.md").chmod(0o755)

    with pytest.raises(export_public_tree.ExportError, match=r"mode mismatch.*README\.md"):
        export_public_tree.verify_export(repo, destination, manifest_path, policy)


def test_export_rejects_included_symlink(tmp_path: Path) -> None:
    """The initial public policy cannot materialize a symlink or an escape target."""
    repo, _, policy = _repository(tmp_path)
    (repo / "escape").symlink_to("../outside")
    _git(repo, "add", "escape")
    _git(repo, "commit", "-qm", "add symlink")
    revision = _git(repo, "rev-parse", "HEAD")
    _policy(policy, public_patterns=["README.md", "escape"])
    _git(repo, "add", policy.relative_to(repo).as_posix())
    _git(repo, "commit", "-qm", "include symlink in policy")
    revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(export_public_tree.ExportError, match=r"unsupported included mode 120000.*escape"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")


def test_export_recurses_and_matches_git_tree_identity(tmp_path: Path) -> None:
    """The manifest tree is the Git tree produced from all exported paths and modes."""
    repo, _, policy = _repository(tmp_path)
    nested = repo / "src" / "example"
    nested.mkdir(parents=True)
    executable = nested / "tool.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    _git(repo, "add", "src/example/tool.py")
    _git(repo, "commit", "-qm", "add nested executable")
    revision = _git(repo, "rev-parse", "HEAD")
    _policy(policy, public_patterns=["README.md", "src/**"])
    _git(repo, "add", policy.relative_to(repo).as_posix())
    _git(repo, "commit", "-qm", "include nested source in policy")
    revision = _git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"

    manifest = export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    assert export_public_tree.verify_export(repo, destination, manifest_path, policy) == manifest
    _git(destination, "init", "-q")
    _git(destination, "add", "-f", ".")

    assert _git(destination, "write-tree") == manifest.exported_tree


def test_verify_rejects_empty_git_directory(tmp_path: Path) -> None:
    """Even empty repository metadata invalidates a clean-history export."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    (destination / ".git").mkdir()

    with pytest.raises(export_public_tree.ExportError, match="forbidden Git repository state"):
        export_public_tree.verify_export(repo, destination, manifest_path, policy)


def test_verify_rejects_schema_invalid_manifest_identity(tmp_path: Path) -> None:
    """Malformed producer identity fields fail before any digest is trusted."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_commit"] = "not-an-object-id"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(export_public_tree.ExportError, match=r"export manifest schema violation \(pattern\)"):
        export_public_tree.verify_export(repo, destination, manifest_path, policy)


def test_export_ignores_git_replacement_refs(tmp_path: Path) -> None:
    """Local replacement refs cannot substitute another commit's bytes or identity."""
    repo, original, policy = _repository(tmp_path)
    (repo / "README.md").write_text("replacement\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "replacement candidate")
    replacement = _git(repo, "rev-parse", "HEAD")
    _git(repo, "replace", original, replacement)

    manifest = export_public_tree.export_tree(repo, original, policy, tmp_path / "public", tmp_path / "manifest.json")

    assert (tmp_path / "public" / "README.md").read_text(encoding="utf-8") == "public\n"
    assert manifest.source_tree == _git(repo, "--no-replace-objects", "rev-parse", f"{original}^{{tree}}")


def test_export_ignores_ambient_git_repository_redirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller Git variables cannot redirect object reads away from the requested repository."""
    repo, revision, policy = _repository(tmp_path / "intended")
    decoy, _, _ = _repository(tmp_path / "decoy")
    (decoy / "README.md").write_text("decoy\n", encoding="utf-8")
    _git(decoy, "commit", "-qam", "change decoy")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))

    manifest = export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")

    assert manifest.source_commit == revision
    assert (tmp_path / "public" / "README.md").read_text(encoding="utf-8") == "public\n"


def test_verify_rejects_fifo(tmp_path: Path) -> None:
    """Non-file filesystem objects cannot hide outside the manifest inventory."""
    repo, revision, policy = _repository(tmp_path)
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_public_tree.export_tree(repo, revision, policy, destination, manifest_path)
    os.mkfifo(destination / "unexpected-fifo")

    with pytest.raises(export_public_tree.ExportError, match=r"unsupported filesystem entry.*unexpected-fifo"):
        export_public_tree.verify_export(repo, destination, manifest_path, policy)


def test_export_does_not_follow_predictable_manifest_temporary_symlink(tmp_path: Path) -> None:
    """A planted legacy temporary symlink cannot redirect manifest writes."""
    repo, revision, policy = _repository(tmp_path)
    protected = tmp_path / "protected.txt"
    protected.write_text("preserve me\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.with_suffix(".json.tmp").symlink_to(protected)

    export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", manifest_path)

    assert protected.read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize("field", ["schema_version", "unknown-private-field"])
def test_schema_errors_do_not_reflect_supplied_values(field: str) -> None:
    schema_path = Path(__file__).parents[1] / "docs/release-readiness/public-history-source.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    value: dict[str, object] = {}
    value.update(dict.fromkeys(schema["required"]))
    value[field] = "private-schema-sentinel\x1b[31m" * 1000
    with pytest.raises(export_public_tree.ExportError, match="schema violation") as caught:
        export_public_tree._validate_schema(value, schema_path, "source")
    assert "private-schema-sentinel" not in str(caught.value)
    if field == "unknown-private-field":
        assert field not in str(caught.value)
    assert len(str(caught.value)) < 160
    assert caught.value.__cause__ is None
