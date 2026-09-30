"""Complete public descendant snapshots, bound to an independently approved cutover.

The trusted caller supplies the historical anchor and an independently observed
current repository ID. Matching a local origin and caller-supplied ID does not
authenticate GitHub, approve a release, or prove current workflow execution.
This contract establishes local source identity only. It neither generates an
anchor from a supplied record nor changes the frozen initial-export policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING or __package__:
    from scripts import export_public_tree, git_worktree, quality_source, release_approval_archive
else:  # pragma: no cover - exercised by direct-script subprocess tests
    import export_public_tree
    import git_worktree
    import quality_source
    import release_approval_archive

SCHEMA_VERSION = 1
SOURCE_KIND: Literal["public-history"] = "public-history"
MAX_SOURCE_BYTES = quality_source.GIT_OUTPUT_LIMIT_BYTES
_MAX_PATH_DEPTH = 128


@dataclass(frozen=True)
class ApprovedCutoverAnchor:
    """Historical identity obtained from the caller's independent approval store."""

    repository: str
    repository_id: int
    initial_commit: str
    initial_tree: str
    record_sha256: str


@dataclass(frozen=True)
class PublicHistorySource:
    """Versioned source contract; entries cover the complete current Git tree."""

    schema_version: int
    source_kind: Literal["public-history"]
    anchor: ApprovedCutoverAnchor
    repository: str
    repository_id: int
    source_commit: str
    source_tree: str
    version: str
    planned_tag: str
    policy_path: str
    policy_oid: str
    policy_sha256: str
    entries: tuple[export_public_tree.TreeEntry, ...]


def _git(repo: Path, *args: str) -> bytes:
    return quality_source._git(repo, "--no-replace-objects", *args)


def _release_version(version: str, planned_tag: str) -> None:
    if len(version) > 32 or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("public history version must be a release version")
    if tuple(int(part) for part in version.split(".")) <= (1, 0, 0):
        raise ValueError("public history version must follow the initial v1.0.0 release")
    if planned_tag != f"v{version}":
        raise ValueError("planned tag must match the source version")


def _entry_paths(entries: tuple[export_public_tree.TreeEntry, ...]) -> None:
    if not entries or len(entries) > quality_source.MAX_ENTRY_COUNT:
        raise ValueError("public history entry inventory exceeds size bound")
    if entries != tuple(sorted(entries)):
        raise ValueError("public history entries must be in canonical order")
    for entry in entries:
        path = release_approval_archive._member_path(entry.path)
        if (
            path.as_posix() != entry.path
            or ".git" in path.parts
            or any(part.casefold() == ".git" for part in path.parts)
        ):
            raise ValueError("public history path is unsafe or noncanonical")
        if len(entry.path.encode("utf-8")) > quality_source.MAX_PATH_BYTES or len(path.parts) > _MAX_PATH_DEPTH:
            raise ValueError("public history path exceeds size bound")


def _field_bounds(source: PublicHistorySource) -> None:
    if not source.entries or len(source.entries) > quality_source.MAX_ENTRY_COUNT:
        raise ValueError("public history entry inventory exceeds size bound")
    fields = (
        (source.source_kind, 14),
        (source.repository, 128),
        (source.source_commit, 40),
        (source.source_tree, 40),
        (source.version, 32),
        (source.planned_tag, 33),
        (source.policy_path, 128),
        (source.policy_oid, 40),
        (source.policy_sha256, 64),
        (source.anchor.repository, 128),
        (source.anchor.initial_commit, 40),
        (source.anchor.initial_tree, 40),
        (source.anchor.record_sha256, 64),
    )
    if any(not isinstance(value, str) or len(value) > limit for value, limit in fields):
        raise ValueError("public history field exceeds size bound or has an invalid type")
    for entry in source.entries:
        entry_fields = (
            (entry.path, quality_source.MAX_PATH_BYTES),
            (entry.mode, 6),
            (entry.oid, 40),
            (entry.category, 128),
            (entry.rule_id, 128),
        )
        if any(not isinstance(value, str) or len(value) > limit for value, limit in entry_fields):
            raise ValueError("public history entry field exceeds size bound or has an invalid type")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", entry.category) or not re.fullmatch(r"[a-z0-9-]+", entry.rule_id):
            raise ValueError("public history entry classification identity is invalid")
    if type(source.schema_version) is not int or source.schema_version != SCHEMA_VERSION:
        raise ValueError("public history schema version must be an exact supported integer")
    if any(
        type(value) is not int or not 1 <= value <= 9007199254740991
        for value in (source.repository_id, source.anchor.repository_id)
    ):
        raise ValueError("public history repository IDs must be exact bounded integers")


def _validate_source(source: PublicHistorySource, anchor: ApprovedCutoverAnchor, record: bytes) -> None:
    _field_bounds(source)
    _entry_paths(source.entries)
    export_public_tree._validate_schema(
        _source_dict(source),
        Path(__file__).resolve().parent.parent / "docs/release-readiness/public-history-source.schema.json",
        "public history source",
    )
    _anchor_record(record, anchor)
    if source.anchor != anchor:
        raise ValueError("retained anchor differs from independently approved anchor")
    if source.repository != anchor.repository or source.repository_id != anchor.repository_id:
        raise ValueError("current repository identity differs from independently approved anchor")
    if source.source_commit == anchor.initial_commit:
        raise ValueError("public history source must be a descendant of the initial commit")
    _release_version(source.version, source.planned_tag)
    if export_public_tree._tree_oid(source.entries) != source.source_tree:
        raise ValueError("public history inventory does not reconstruct the complete committed tree")


def _source_dict(source: PublicHistorySource) -> dict[str, object]:
    raw = asdict(source)
    raw["entries"] = list(raw["entries"])
    return raw


def _canonical_bytes(source: PublicHistorySource) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    output = bytearray()
    for fragment in encoder.iterencode(_source_dict(source)):
        encoded = fragment.encode("utf-8")
        if len(output) + len(encoded) + 1 > MAX_SOURCE_BYTES:
            raise ValueError("public history source exceeds size bound")
        output.extend(encoded)
    output.append(10)
    return bytes(output)


def source_bytes(
    source: PublicHistorySource, *, expected_anchor: ApprovedCutoverAnchor, cutover_record: bytes
) -> bytes:
    """Serialize validated local identity as sorted, compact ASCII JSON plus LF.

    The independent anchor and exact historical record are mandatory. Validation
    here does not observe Git ancestry, current GitHub identity, or workflow
    authority; use verify_source for the current local repository and snapshot.
    """
    _validate_source(source, expected_anchor, cutover_record)
    return _canonical_bytes(source)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("public history source identity must be an object")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("public history source identity must be a string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("public history identity integers must be exact integers")
    return value


def source_from_bytes(
    payload: bytes, *, expected_anchor: ApprovedCutoverAnchor, cutover_record: bytes
) -> PublicHistorySource:
    """Read one bounded canonical contract, not a live approval or ancestry proof.

    Tree reconstruction checks entries and modes, but retained classification
    labels, policy digests, and descendant ancestry require current verify_source.
    No anchor is inferred or approved from the retained JSON or historical record.
    """
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError("public history source exceeds size bound")
    try:
        raw = export_public_tree._json_object(payload.decode("utf-8"), "public history source")
        export_public_tree._validate_schema(
            raw,
            Path(__file__).resolve().parent.parent / "docs/release-readiness/public-history-source.schema.json",
            "public history source",
        )
        anchor = _object(raw["anchor"])
        values = raw["entries"]
        if not isinstance(values, list):
            raise ValueError("public history entries must be a list")
        entries: list[export_public_tree.TreeEntry] = []
        for value in values:
            entry = _object(value)
            entries.append(
                export_public_tree.TreeEntry(
                    path=_string(entry["path"]),
                    mode=_string(entry["mode"]),
                    oid=_string(entry["oid"]),
                    category=_string(entry["category"]),
                    rule_id=_string(entry["rule_id"]),
                )
            )
        source = PublicHistorySource(
            schema_version=_integer(raw["schema_version"]),
            source_kind=SOURCE_KIND,
            anchor=ApprovedCutoverAnchor(
                repository=_string(anchor["repository"]),
                repository_id=_integer(anchor["repository_id"]),
                initial_commit=_string(anchor["initial_commit"]),
                initial_tree=_string(anchor["initial_tree"]),
                record_sha256=_string(anchor["record_sha256"]),
            ),
            repository=_string(raw["repository"]),
            repository_id=_integer(raw["repository_id"]),
            source_commit=_string(raw["source_commit"]),
            source_tree=_string(raw["source_tree"]),
            version=_string(raw["version"]),
            planned_tag=_string(raw["planned_tag"]),
            policy_path=_string(raw["policy_path"]),
            policy_oid=_string(raw["policy_oid"]),
            policy_sha256=_string(raw["policy_sha256"]),
            entries=tuple(entries),
        )
        _validate_source(source, expected_anchor, cutover_record)
        if payload != _canonical_bytes(source):
            raise ValueError("public history source bytes are not canonical")
        return source
    except RecursionError:
        raise ValueError("public history source exceeds nesting limit") from None
    except UnicodeError:
        raise ValueError("public history source is not bounded UTF-8 JSON") from None


def load_source(path: Path, *, expected_anchor: ApprovedCutoverAnchor, cutover_record: bytes) -> PublicHistorySource:
    """Load a bounded regular contract below pinned, no-follow directories."""
    try:
        with ExitStack() as descriptors:
            parent_fd = release_approval_archive._open_real_directory(path.parent, create=False)
            descriptors.callback(os.close, parent_fd)
            payload, _info = export_public_tree._read_file_at(parent_fd, path.name)
            source = source_from_bytes(payload, expected_anchor=expected_anchor, cutover_record=cutover_record)
            export_public_tree._require_parent_binding(path.parent, parent_fd)
    except OSError as error:
        raise ValueError("cannot load stable regular public history source") from error
    return source


def _anchor_record(raw: bytes, anchor: ApprovedCutoverAnchor) -> None:
    if len(raw) > quality_source.GIT_OUTPUT_LIMIT_BYTES:
        raise ValueError("cutover record exceeds size bound")
    if len(anchor.repository) > 128 or not re.fullmatch(r"[a-z0-9-]+/fieldkit-cli", anchor.repository):
        raise ValueError("approved anchor repository is invalid")
    if type(anchor.repository_id) is not int or not 1 <= anchor.repository_id <= 9007199254740991:
        raise ValueError("approved anchor repository ID is invalid")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (anchor.initial_commit, anchor.initial_tree)):
        raise ValueError("approved anchor commit or tree is invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", anchor.record_sha256)
        or hashlib.sha256(raw).hexdigest() != anchor.record_sha256
    ):
        raise ValueError("cutover record digest differs from independently approved anchor")
    value = export_public_tree._json_object(raw.decode("utf-8"), "cutover record")
    if not isinstance(value, dict):
        raise ValueError("cutover record must be an object")
    export_public_tree._validate_schema(
        value,
        Path(__file__).resolve().parent.parent / "docs/release-readiness/cutover-record.schema.json",
        "cutover record",
    )
    public = value["public"]
    candidate = value["candidate"]
    if not isinstance(public, dict) or not isinstance(candidate, dict):
        raise ValueError("cutover record identities must be objects")
    if (
        public["initial_commit"] != anchor.initial_commit
        or public["initial_tree"] != anchor.initial_tree
        or public["repository_id"] != anchor.repository_id
        or candidate["repository"] != anchor.repository
        or candidate["exported_tree"] != anchor.initial_tree
        or candidate["planned_tag"] != "v1.0.0"
    ):
        raise ValueError("cutover record identity differs from approved anchor")
    if any(run["head_sha"] != anchor.initial_commit for run in public["workflow_runs"]):
        raise ValueError("cutover record workflow identity differs from approved anchor")


def _history_identity(repo: Path, commit: str, anchor: ApprovedCutoverAnchor, repository_id: int) -> None:
    if type(repository_id) is not int or repository_id != anchor.repository_id:
        raise ValueError("current repository ID differs from approved anchor")
    origins = {
        f"https://github.com/{anchor.repository}",
        f"https://github.com/{anchor.repository}.git",
        f"git@github.com:{anchor.repository}.git",  # pii-guard: ignore - Git SSH service account
        f"ssh://git@github.com/{anchor.repository}.git",  # pii-guard: ignore - Git SSH service account
    }
    if _git(repo, "remote", "get-url", "origin").decode().strip() not in origins:
        raise ValueError("current repository origin differs from approved anchor")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("public history requires complete ancestry")
    grafts = Path(_git(repo, "rev-parse", "--git-path", "info/grafts").decode().strip())
    if not grafts.is_absolute():
        grafts = repo / grafts
    if grafts.exists() or grafts.is_symlink():
        raise ValueError("public history rejects grafted ancestry")
    initial, initial_tree = export_public_tree._resolve_commit(repo, anchor.initial_commit)
    if (initial, initial_tree) != (anchor.initial_commit, anchor.initial_tree):
        raise ValueError("approved anchor root or tree differs from Git history")
    roots = _git(repo, "rev-list", "--max-parents=0", commit).decode().splitlines()
    if roots != [anchor.initial_commit]:
        raise ValueError("public history must descend from the sole approved root")
    if commit == initial:
        raise ValueError("public history source must be a descendant of the initial commit")


def capture_source(
    repo: Path,
    revision: str,
    *,
    expected_anchor: ApprovedCutoverAnchor,
    cutover_record: bytes,
    repository_id: int,
    version: str,
    planned_tag: str,
) -> PublicHistorySource:
    """Bind a clean, complete descendant tree to independently trusted identity.

    `repository_id` is current identity observed by the trusted caller, not an
    authenticated observation made here. `planned_tag` is release intent; this
    function does not require or create a Git tag or authorize publication.
    """
    repo = repo.resolve(strict=True)
    _anchor_record(cutover_record, expected_anchor)
    git_worktree.require_clean_worktree(repo)
    commit, tree = export_public_tree._resolve_commit(repo, revision)
    if commit != git_worktree.head_revision(repo):
        raise ValueError("source must be the current clean HEAD descendant")
    _history_identity(repo, commit, expected_anchor, repository_id)
    _release_version(version, planned_tag)
    project = tomllib.loads(_git(repo, "show", f"{commit}:pyproject.toml").decode("utf-8")).get("project")
    if not isinstance(project, dict) or project.get("version") != version:
        raise ValueError("release version differs from committed project version")
    policy, policy_path, policy_oid, policy_sha256 = export_public_tree._load_committed_policy(
        repo, commit, repo / "docs/release-readiness/public-tree-policy.json"
    )
    if policy.expected_repository != expected_anchor.repository:
        raise ValueError("committed policy repository differs from approved anchor")
    raw_entries = export_public_tree._source_entries(repo, commit)
    if len(raw_entries) > quality_source.MAX_ENTRY_COUNT:
        raise ValueError("public history entry inventory exceeds size bound")
    entries, excluded = export_public_tree._classify(raw_entries, policy)
    if excluded:
        raise ValueError("public history contains excluded tracked paths; filtering is forbidden")
    total_bytes = 0
    for entry in entries:
        release_approval_archive._member_path(entry.path)
        if len(entry.path.encode("utf-8")) > quality_source.MAX_PATH_BYTES:
            raise ValueError("public history path exceeds size bound")
        size = int(_git(repo, "cat-file", "-s", entry.oid))
        if size > min(quality_source.MAX_FILE_BYTES, quality_source.GIT_OUTPUT_LIMIT_BYTES):
            raise ValueError("public history blob exceeds size bound")
        total_bytes += size
    if total_bytes > quality_source.MAX_WORKTREE_BYTES:
        raise ValueError("public history snapshot exceeds size bound")
    if export_public_tree._tree_oid(entries) != tree:
        raise ValueError("public history inventory does not reconstruct the complete committed tree")
    git_worktree.require_clean_worktree(repo)
    if git_worktree.head_revision(repo) != commit:
        raise ValueError("public history source changed during capture")
    source = PublicHistorySource(
        schema_version=SCHEMA_VERSION,
        source_kind=SOURCE_KIND,
        anchor=expected_anchor,
        repository=expected_anchor.repository,
        repository_id=repository_id,
        source_commit=commit,
        source_tree=tree,
        version=version,
        planned_tag=planned_tag,
        policy_path=policy_path,
        policy_oid=policy_oid,
        policy_sha256=policy_sha256,
        entries=entries,
    )
    _validate_source(source, expected_anchor, cutover_record)
    return source


def _require_contract(repo: Path, source: PublicHistorySource, anchor: ApprovedCutoverAnchor, record: bytes) -> None:
    current = capture_source(
        repo,
        source.source_commit,
        expected_anchor=anchor,
        cutover_record=record,
        repository_id=source.repository_id,
        version=source.version,
        planned_tag=source.planned_tag,
    )
    if current != source:
        raise ValueError("public history contract differs from independently reconstructed source")


def materialize_source(
    repo: Path,
    source: PublicHistorySource,
    destination: Path,
    *,
    expected_anchor: ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> PublicHistorySource:
    """Publish a complete snapshot with existing FD-safe no-replace utilities."""
    _require_contract(repo, source, expected_anchor, cutover_record)
    if destination.resolve().is_relative_to(repo.resolve()) or repo.resolve().is_relative_to(destination.resolve()):
        raise ValueError("snapshot destination must be outside the source repository")
    with ExitStack() as descriptors:
        parent_fd = release_approval_archive._open_real_directory(destination.parent, create=True)
        descriptors.callback(os.close, parent_fd)
        export_public_tree._require_absent(parent_fd, destination.name, "destination")
        name, root_fd = release_approval_archive._create_staging_directory(parent_fd, destination.name)
        descriptors.callback(os.close, root_fd)
        for entry in source.entries:
            relative = release_approval_archive._member_path(entry.path)
            directory_fd = release_approval_archive._open_or_create_member_directory(root_fd, relative.parts[:-1])
            try:
                export_public_tree._write_file_at(
                    directory_fd,
                    relative.parts[-1],
                    _git(repo, "cat-file", "blob", entry.oid),
                    0o755 if entry.mode == "100755" else 0o644,
                )
            finally:
                os.close(directory_fd)
        export_public_tree._verify_tree_at(root_fd, source.entries)
        _require_contract(repo, source, expected_anchor, cutover_record)
        export_public_tree._require_directory_binding(parent_fd, name, root_fd)
        export_public_tree._require_parent_binding(destination.parent, parent_fd)
        release_approval_archive._rename_no_replace_at(parent_fd, name, destination.name)
        export_public_tree._require_directory_binding(parent_fd, destination.name, root_fd)
        export_public_tree._verify_tree_at(root_fd, source.entries)
        export_public_tree._require_parent_binding(destination.parent, parent_fd)
    return source


def verify_source(
    repo: Path,
    source: PublicHistorySource,
    destination: Path,
    *,
    expected_anchor: ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> PublicHistorySource:
    """Reconstruct the contract and verify exact snapshot paths, bytes, and modes."""
    _require_contract(repo, source, expected_anchor, cutover_record)
    with ExitStack() as descriptors:
        parent_fd = release_approval_archive._open_real_directory(destination.parent, create=False)
        descriptors.callback(os.close, parent_fd)
        root_fd = release_approval_archive._open_directory_at(parent_fd, destination.name)
        descriptors.callback(os.close, root_fd)
        export_public_tree._verify_tree_at(root_fd, source.entries)
        export_public_tree._require_directory_binding(parent_fd, destination.name, root_fd)
        export_public_tree._require_parent_binding(destination.parent, parent_fd)
    _require_contract(repo, source, expected_anchor, cutover_record)
    return source
