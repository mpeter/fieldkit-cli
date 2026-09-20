"""Tests for deterministic, fail-closed clean public-tree export."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import export_public_tree

pytestmark = pytest.mark.unit


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


def test_export_rejects_duplicate_policy_keys(tmp_path: Path) -> None:
    """Duplicate JSON keys cannot silently replace reviewed policy values."""
    repo, _, policy = _repository(tmp_path)
    policy.write_text(
        '{"schema_version":1,"schema_version":1,"expected_repository":"example/fieldkit-cli"}',
        encoding="utf-8",
    )
    _git(repo, "add", policy.relative_to(repo).as_posix())
    _git(repo, "commit", "-qm", "commit malformed policy")
    revision = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(export_public_tree.ExportError, match="duplicate JSON key: schema_version"):
        export_public_tree.export_tree(repo, revision, policy, tmp_path / "public", tmp_path / "manifest.json")


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

    with pytest.raises(export_public_tree.ExportError, match=r"schema violation.*source_commit"):
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
