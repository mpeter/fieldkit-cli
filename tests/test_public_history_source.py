"""The descendant contract retains every public file and its approved root."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import export_public_tree, public_history_source, quality_source

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("script", ["release_workflow_policy.py", "check_public_tree_safety.py"])
def test_public_history_consumers_support_direct_script_invocation(tmp_path: Path, script: str) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not result.stderr


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def _commit(repo: Path) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test snapshot")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Example Contributor")
    _git(repo, "config", "user.email", "contributor@example.com")
    _git(repo, "remote", "add", "origin", "https://github.com/example/fieldkit-cli.git")
    policy = repo / "docs/release-readiness/public-tree-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_repository": "example/fieldkit-cli",
                "planned_tag": "v1.0.0",
                "rules": [
                    {
                        "id": "public",
                        "action": "include",
                        "category": "product",
                        "patterns": [
                            "README.md",
                            "pyproject.toml",
                            "docs/release-readiness/public-tree-policy.json",
                            "src/**",
                        ],
                        "rationale": "Reviewed public files",
                    },
                    {
                        "id": "internal",
                        "action": "exclude",
                        "category": "private_history",
                        "patterns": ["docs/audit/**"],
                        "rationale": "Private audit records",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "example-cli"\nversion = "1.0.0"\n', encoding="utf-8")
    initial = _commit(repo)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    record = json.dumps(
        {
            "schema_version": 1,
            "candidate": {
                "repository": "example/fieldkit-cli",
                "source_sha": "a" * 40,
                "source_tree": "b" * 40,
                "export_policy_sha256": "c" * 64,
                "exported_tree": tree,
                "planned_tag": "v1.0.0",
                "artifacts": [
                    {"name": "example.whl", "kind": "wheel", "sha256": "d" * 64},
                    {"name": "example.tar.gz", "kind": "sdist", "sha256": "e" * 64},
                ],
            },
            "public": {
                "initial_commit": initial,
                "initial_tree": tree,
                "repository_id": 42,
                "workflow_runs": [
                    {
                        "name": "Cutover verification",
                        "path": ".github/workflows/cutover.yml",
                        "event": "push",
                        "id": 1,
                        "attempt": 1,
                        "head_sha": initial,
                        "conclusion": "success",
                    }
                ],
            },
        }
    ).encode()
    anchor = public_history_source.ApprovedCutoverAnchor(
        repository="example/fieldkit-cli",
        repository_id=42,
        initial_commit=initial,
        initial_tree=tree,
        record_sha256=hashlib.sha256(record).hexdigest(),
    )
    (repo / "README.md").write_text("successor\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "example-cli"\nversion = "1.1.0"\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/new.py").write_text('"""New public file."""\n', encoding="utf-8")
    _commit(repo)
    return repo, anchor, record


def _capture(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
) -> public_history_source.PublicHistorySource:
    repo, anchor, record = history
    return public_history_source.capture_source(
        repo,
        "HEAD",
        expected_anchor=anchor,
        cutover_record=record,
        repository_id=42,
        version="1.1.0",
        planned_tag="v1.1.0",
    )


def test_changed_descendant_retains_full_tree_and_historical_anchor(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], tmp_path: Path
) -> None:
    repo, anchor, record = history
    source = _capture(history)
    assert source.source_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert source.source_tree != anchor.initial_tree
    assert source.anchor == anchor
    assert source.schema_version == 1
    assert source.source_kind == "public-history"
    destination = tmp_path / "snapshot"
    assert (
        public_history_source.materialize_source(
            repo, source, destination, expected_anchor=anchor, cutover_record=record
        )
        == source
    )
    assert (
        public_history_source.verify_source(repo, source, destination, expected_anchor=anchor, cutover_record=record)
        == source
    )
    assert not (destination / ".git").exists()
    assert {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()} == {
        entry.path for entry in source.entries
    }


@pytest.mark.parametrize("mutation", ["dirty", "staged", "untracked", "mode", "index-flag"])
def test_dirty_sources_are_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], mutation: str
) -> None:
    repo, _, _ = history
    if mutation == "untracked":
        (repo / "new.txt").write_text("untracked", encoding="utf-8")
    elif mutation == "mode":
        (repo / "README.md").chmod(0o755)
    elif mutation == "index-flag":
        _git(repo, "update-index", "--assume-unchanged", "README.md")
    else:
        (repo / "README.md").write_text("dirty", encoding="utf-8")
        if mutation == "staged":
            _git(repo, "add", "README.md")
    with pytest.raises(ValueError, match=r"clean worktree|index flags"):
        _capture(history)


@pytest.mark.parametrize("mutation", ["added", "omitted", "mode", "bytes", "symlink", "git", "empty-directory"])
def test_snapshot_changes_are_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], tmp_path: Path, mutation: str
) -> None:
    repo, anchor, record = history
    source = _capture(history)
    destination = tmp_path / "snapshot"
    public_history_source.materialize_source(repo, source, destination, expected_anchor=anchor, cutover_record=record)
    path = destination / "README.md"
    if mutation == "added":
        (destination / "extra.txt").write_text("extra", encoding="utf-8")
    elif mutation == "omitted":
        path.unlink()
    elif mutation == "mode":
        path.chmod(0o600)
    elif mutation == "bytes":
        path.write_text("altered", encoding="utf-8")
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(repo / "README.md")
    else:
        (destination / (".git" if mutation == "git" else "empty")).mkdir()
    with pytest.raises(ValueError, match=r"export member|inventory"):
        public_history_source.verify_source(repo, source, destination, expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_sha256", "0" * 64),
        ("initial_commit", "0" * 40),
        ("initial_tree", "0" * 40),
        ("repository_id", 43),
        ("repository", "example/wrong"),
    ],
)
def test_wrong_trusted_anchor_is_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], field: str, value: str | int
) -> None:
    repo, anchor, record = history
    with pytest.raises(ValueError, match=r"anchor|record|repository"):
        if field == "repository_id":
            assert isinstance(value, int)
            changed = replace(anchor, repository_id=value)
        else:
            assert isinstance(value, str)
            if field == "repository":
                changed = replace(anchor, repository=value)
            elif field == "initial_commit":
                changed = replace(anchor, initial_commit=value)
            elif field == "initial_tree":
                changed = replace(anchor, initial_tree=value)
            else:
                changed = replace(anchor, record_sha256=value)
        _capture((repo, changed, record))


def test_raw_record_bytes_must_match_trusted_digest(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
) -> None:
    repo, anchor, record = history
    with pytest.raises(ValueError, match="digest"):
        _capture((repo, anchor, record + b"\n"))


def test_duplicate_record_keys_are_rejected_even_with_matching_digest(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
) -> None:
    repo, anchor, record = history
    record = record.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    anchor = replace(anchor, record_sha256=hashlib.sha256(record).hexdigest())
    with pytest.raises(ValueError, match="cannot load cutover record: invalid JSON input"):
        _capture((repo, anchor, record))


@pytest.mark.parametrize("mutation", ["non-root", "wrong-tree"])
def test_trusted_record_must_also_match_local_root_history(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    mutation: str,
) -> None:
    repo, anchor, record = history
    value = json.loads(record)
    if mutation == "non-root":
        commit = _git(repo, "rev-parse", "HEAD")
        tree = _git(repo, "rev-parse", "HEAD^{tree}")
        value["public"]["initial_commit"] = commit
        value["public"]["workflow_runs"][0]["head_sha"] = commit
        anchor = replace(anchor, initial_commit=commit)
    else:
        tree = "0" * 40
    value["public"]["initial_tree"] = tree
    value["candidate"]["exported_tree"] = tree
    record = json.dumps(value).encode()
    anchor = replace(anchor, initial_tree=tree, record_sha256=hashlib.sha256(record).hexdigest())
    with pytest.raises(ValueError, match=r"root|tree"):
        _capture((repo, anchor, record))


@pytest.mark.parametrize("metadata", ["grafts", "shallow"])
def test_incomplete_or_rewritten_ancestry_is_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    metadata: str,
) -> None:
    repo, anchor, _ = history
    target = repo / ".git" / ("info/grafts" if metadata == "grafts" else "shallow")
    target.write_text(anchor.initial_commit + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"grafted ancestry|complete ancestry"):
        _capture(history)


def test_future_public_path_requires_explicit_committed_classification(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
) -> None:
    repo, _, _ = history
    (repo / "future.txt").write_text("public future content", encoding="utf-8")
    _commit(repo)
    with pytest.raises(ValueError, match="unclassified"):
        _capture(history)
    policy_path = repo / "docs/release-readiness/public-tree-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["rules"][0]["patterns"].append("future.txt")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    _commit(repo)
    source = _capture(history)
    assert source.source_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert "future.txt" in {entry.path for entry in source.entries}


@pytest.mark.parametrize("mode", ["symlink", "gitlink"])
def test_unsafe_tracked_modes_are_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    mode: str,
) -> None:
    repo, _, _ = history
    if mode == "symlink":
        (repo / "src/escape.py").symlink_to("../../outside.py")
        _commit(repo)
    else:
        commit = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},src/submodule")
        _git(repo, "commit", "-m", "test gitlink")
        (repo / "src/submodule").mkdir()
    with pytest.raises(ValueError, match=r"unsupported.*mode"):
        _capture(history)


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0", "01.1.0", "1.1.0-rc1"])
def test_successor_version_must_follow_initial_release(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    version: str,
) -> None:
    repo, anchor, record = history
    with pytest.raises(ValueError, match="version"):
        public_history_source.capture_source(
            repo,
            "HEAD",
            expected_anchor=anchor,
            cutover_record=record,
            repository_id=42,
            version=version,
            planned_tag=f"v{version}",
        )


def test_existing_snapshot_output_is_preserved(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    tmp_path: Path,
) -> None:
    repo, anchor, record = history
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "sentinel").write_bytes(b"sentinel")
    with pytest.raises(ValueError, match="must not exist"):
        public_history_source.materialize_source(
            repo,
            _capture(history),
            destination,
            expected_anchor=anchor,
            cutover_record=record,
        )
    assert (destination / "sentinel").read_bytes() == b"sentinel"


@pytest.mark.parametrize("path", ["unknown.txt", "docs/audit/private.md"])
def test_public_history_cannot_filter_unapproved_tracked_paths(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], path: str
) -> None:
    repo, _, _ = history
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unapproved", encoding="utf-8")
    _commit(repo)
    with pytest.raises(ValueError, match=r"unclassified|excluded"):
        _capture(history)


def test_non_descendant_is_rejected(history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes]) -> None:
    repo, _, _ = history
    _git(repo, "checkout", "--orphan", "unrelated")
    _commit(repo)
    with pytest.raises(ValueError, match=r"root|descendant"):
        _capture(history)


def test_merge_with_unrelated_root_is_rejected(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
) -> None:
    repo, _, _ = history
    old_branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "--orphan", "unrelated")
    _commit(repo)
    _git(repo, "checkout", old_branch)
    _git(repo, "merge", "--allow-unrelated-histories", "--no-edit", "unrelated")
    with pytest.raises(ValueError, match="sole approved root"):
        _capture(history)


@pytest.mark.parametrize("mutation", ["repository", "id", "version", "tag", "initial", "manifest"])
def test_current_identity_and_contract_are_bound(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes], tmp_path: Path, mutation: str
) -> None:
    repo, anchor, record = history
    if mutation == "repository":
        _git(repo, "remote", "set-url", "origin", "https://github.com/example/other.git")
    with pytest.raises(ValueError, match=r"repository|version|tag|descendant|contract"):
        if mutation == "manifest":
            source = replace(_capture(history), entries=())
            public_history_source.materialize_source(
                repo, source, tmp_path / "snapshot", expected_anchor=anchor, cutover_record=record
            )
        else:
            public_history_source.capture_source(
                repo,
                anchor.initial_commit if mutation == "initial" else "HEAD",
                expected_anchor=anchor,
                cutover_record=record,
                repository_id=43 if mutation == "id" else 42,
                version="1.2.0" if mutation == "version" else "1.1.0",
                planned_tag="v1.2.0" if mutation == "tag" else "v1.1.0",
            )


def test_oversized_committed_policy_is_rejected_before_parse(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = history
    policy = repo / "docs/release-readiness/public-tree-policy.json"
    policy.write_bytes(policy.read_bytes() + b" " * (2 * 1024 * 1024))
    _commit(repo)
    original_validate = export_public_tree._validate_schema

    def reject_parse(value: dict[str, object], path: Path, subject: str) -> None:
        assert subject != "public-tree policy", "oversized policy reached its parser"
        original_validate(value, path, subject)

    monkeypatch.setattr(export_public_tree, "_validate_schema", reject_parse)
    with pytest.raises(ValueError, match=r"policy.*size bound"):
        _capture(history)


def test_inventory_limit_is_enforced_before_classification(
    history: tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quality_source, "MAX_ENTRY_COUNT", 2)

    def reject_classification(*args: object) -> None:
        raise AssertionError("oversized inventory reached classification")

    monkeypatch.setattr(export_public_tree, "_classify", reject_classification)
    with pytest.raises(ValueError, match=r"inventory.*bound"):
        _capture(history)
