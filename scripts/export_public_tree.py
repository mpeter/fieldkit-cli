#!/usr/bin/env python3
"""Export and independently verify fieldkit's deterministic clean public tree."""

import argparse
import fnmatch
import hashlib
import json
import os
import secrets
import stat
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from fieldkit.util.bounded_process import BoundedProcessError
from fieldkit.util.text_snapshot import read_text_snapshot

if TYPE_CHECKING or __package__:
    from scripts import quality_source, release_approval_archive
    from scripts.json_policy import load_json_bytes
else:
    import quality_source
    import release_approval_archive
    from json_policy import load_json_bytes

_REPO_ROOT = Path(__file__).resolve().parent.parent
_POLICY_PATH = Path("docs/release-readiness/public-tree-policy.json")
_POLICY_SCHEMA_PATH = _REPO_ROOT / "docs/release-readiness/public-tree-policy.schema.json"
_MANIFEST_SCHEMA_PATH = _REPO_ROOT / "docs/release-readiness/public-tree-manifest.schema.json"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_POLICY_BYTES = 2 * 1024 * 1024
_REGULAR_MODES = frozenset({"100644", "100755"})
_TRACKED_MODES = frozenset({*_REGULAR_MODES, "120000"})
_ACTIONS = frozenset({"include", "exclude"})


class ExportError(ValueError):
    """Raised when an export or verification fails closed."""


@dataclass(frozen=True, order=True)
class Rule:
    """One reviewed public-tree classification rule."""

    id: str
    action: str
    category: str
    patterns: tuple[str, ...]
    rationale: str


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One classified Git tree entry."""

    path: str
    mode: str
    oid: str
    category: str
    rule_id: str


@dataclass(frozen=True)
class ExportManifest:
    """Identity binding for one clean public export."""

    schema_version: int
    source_commit: str
    source_tree: str
    policy_path: str
    policy_oid: str
    policy_sha256: str
    expected_repository: str
    planned_tag: str
    exported_tree: str
    included: tuple[TreeEntry, ...]
    excluded: tuple[TreeEntry, ...]


@dataclass(frozen=True)
class Policy:
    """Validated public-tree policy."""

    expected_repository: str
    planned_tag: str
    rules: tuple[Rule, ...]


def _json_object(content: str, subject: str) -> dict[str, object]:
    try:
        value = load_json_bytes(content.encode("utf-8"))
    except (UnicodeError, ValueError):
        raise ExportError(f"cannot load {subject}: invalid JSON input") from None
    if not isinstance(value, dict):
        raise ExportError(f"{subject}: expected a JSON object")
    return value


def _load_json(path: Path, *, maximum_bytes: int = _MAX_JSON_BYTES) -> dict[str, object]:
    try:
        content = read_text_snapshot(path, max_bytes=maximum_bytes).content
    except (OSError, ValueError) as error:
        raise ExportError(f"cannot load {path}: {error}") from error
    return _json_object(content, str(path))


def _validate_schema(value: dict[str, object], schema_path: Path, subject: str) -> None:
    schema = _load_json(schema_path)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except (SchemaError, ValidationError) as error:
        keyword = (
            error.validator
            if isinstance(error.validator, str) and len(error.validator) <= 32 and error.validator.isidentifier()
            else "schema"
        )
        raise ExportError(f"{subject} schema violation ({keyword})") from None


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ExportError(f"public-tree policy {field} must be a non-empty string list")
    return tuple(item for item in value if isinstance(item, str))


def _load_policy(path: Path) -> Policy:
    return _parse_policy(_load_json(path, maximum_bytes=_MAX_POLICY_BYTES))


def _parse_policy(raw: dict[str, object]) -> Policy:
    _validate_schema(raw, _POLICY_SCHEMA_PATH, "public-tree policy")
    if set(raw) != {"schema_version", "expected_repository", "planned_tag", "rules"}:
        raise ExportError("public-tree policy has unsupported top-level fields")
    if raw.get("schema_version") != 1:
        raise ExportError("public-tree policy schema_version must be 1")
    expected_repository = raw.get("expected_repository")
    planned_tag = raw.get("planned_tag")
    raw_rules = raw.get("rules")
    if planned_tag != "v1.0.0":
        raise ExportError("public-tree policy has an unexpected planned tag")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ExportError("public-tree policy rules must be a non-empty list")
    rules: list[Rule] = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict) or set(raw_rule) != {"id", "action", "category", "patterns", "rationale"}:
            raise ExportError(f"public-tree policy rule {index} has unsupported fields")
        identifier = raw_rule.get("id")
        action = raw_rule.get("action")
        category = raw_rule.get("category")
        rationale = raw_rule.get("rationale")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ExportError(f"public-tree policy rule {index} has an empty identifier")
        if not isinstance(action, str) or not action.strip():
            raise ExportError(f"public-tree policy rule {index} has an empty action")
        if not isinstance(category, str) or not category.strip():
            raise ExportError(f"public-tree policy rule {index} has an empty category")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ExportError(f"public-tree policy rule {index} has empty metadata")
        if action not in _ACTIONS:
            raise ExportError(f"public-tree policy rule {identifier} has unsupported action {action}")
        patterns = _strings(raw_rule.get("patterns"), f"rule {identifier} patterns")
        for pattern in patterns:
            parts = PurePosixPath(pattern).parts
            if pattern.startswith("/") or "\\" in pattern or ".." in parts:
                raise ExportError(f"public-tree policy rule {identifier} has unsafe pattern {pattern}")
        rules.append(
            Rule(
                id=identifier,
                action=action,
                category=category,
                patterns=patterns,
                rationale=rationale,
            )
        )
    identifiers = [rule.id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ExportError("public-tree policy rule identifiers must be unique")
    if not isinstance(expected_repository, str) or not isinstance(planned_tag, str):
        raise ExportError("public-tree policy repository and planned tag must be strings")
    return Policy(expected_repository=expected_repository, planned_tag=planned_tag, rules=tuple(rules))


def _git(repo: Path, *args: str) -> bytes:
    try:
        resolved_repo = repo.resolve(strict=True)
    except OSError as error:
        raise ExportError(f"source repository does not exist: {repo}") from error
    try:
        return quality_source._git(resolved_repo, "--no-replace-objects", *args)
    except (OSError, ValueError, BoundedProcessError) as error:
        raise ExportError(f"git {' '.join(args)} failed: {error}") from error


def _resolve_commit(repo: Path, revision: str) -> tuple[str, str]:
    if not revision or revision.startswith("-"):
        raise ExportError("source revision must name an explicit commit")
    commit = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    return commit, tree


def _source_entries(repo: Path, commit: str) -> tuple[tuple[str, str, str], ...]:
    output = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    if output and not output.endswith(b"\0"):
        raise ExportError("git ls-tree inventory is not NUL terminated")
    if output.count(b"\0") > quality_source.MAX_ENTRY_COUNT:
        raise ExportError("git ls-tree inventory exceeds its entry bound")
    entries: list[tuple[str, str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ExportError("git ls-tree returned a malformed record")
        if len(raw_path) > quality_source.MAX_PATH_BYTES:
            raise ExportError("git ls-tree path exceeds its byte bound")
        try:
            mode, object_type, oid = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ExportError("git ls-tree returned an unsupported record") from error
        if object_type != "blob" or mode not in _TRACKED_MODES:
            raise ExportError(f"unsupported tracked mode {mode} for {path}")
        relative = PurePosixPath(path)
        if not path or path.startswith("/") or ".." in relative.parts:
            raise ExportError(f"unsafe tracked path {path}")
        entries.append((path, mode, oid))
    return tuple(entries)


def _classify(
    source_entries: tuple[tuple[str, str, str], ...], policy: Policy
) -> tuple[tuple[TreeEntry, ...], tuple[TreeEntry, ...]]:
    included: list[TreeEntry] = []
    excluded: list[TreeEntry] = []
    for path, mode, oid in source_entries:
        matches = [
            rule for rule in policy.rules if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule.patterns)
        ]
        if not matches:
            raise ExportError(f"unclassified tracked path: {path}")
        if len(matches) > 1:
            identifiers = ", ".join(rule.id for rule in matches)
            raise ExportError(f"ambiguous tracked path: {path} ({identifiers})")
        rule = matches[0]
        entry = TreeEntry(path=path, mode=mode, oid=oid, category=rule.category, rule_id=rule.id)
        if rule.action == "include":
            if mode not in _REGULAR_MODES:
                raise ExportError(f"unsupported included mode {mode} for {path}")
            included.append(entry)
        else:
            excluded.append(entry)
    return tuple(sorted(included)), tuple(sorted(excluded))


def _git_object_id(object_type: str, content: bytes) -> str:
    header = f"{object_type} {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _tree_oid(entries: tuple[TreeEntry, ...]) -> str:
    tree: dict[str, object] = {}
    for entry in entries:
        cursor = tree
        parts = PurePosixPath(entry.path).parts
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise ExportError(f"path collision at {entry.path}")
            cursor = child
        if parts[-1] in cursor:
            raise ExportError(f"duplicate exported path: {entry.path}")
        cursor[parts[-1]] = entry

    def hash_tree(node: dict[str, object]) -> str:
        encoded: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                mode = "40000"
                oid = hash_tree(value)
                sort_name = f"{name}/"
            elif isinstance(value, TreeEntry):
                mode = value.mode
                oid = value.oid
                sort_name = name
            else:
                raise ExportError("invalid tree node")
            record = f"{mode} {name}\0".encode() + bytes.fromhex(oid)
            encoded.append((sort_name.encode(), record))
        content = b"".join(record for _, record in sorted(encoded, key=lambda item: item[0]))
        return _git_object_id("tree", content)

    return hash_tree(tree)


def _load_committed_policy(repo: Path, commit: str, path: Path) -> tuple[Policy, str, str, str]:
    try:
        relative_path = path.resolve(strict=True).relative_to(repo.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise ExportError("public-tree policy must be a file tracked inside the source repository") from error
    if relative_path != _POLICY_PATH.as_posix():
        raise ExportError(f"public-tree policy must use the canonical path: {_POLICY_PATH.as_posix()}")
    oid = _git(repo, "rev-parse", "--verify", f"{commit}:{relative_path}").decode().strip()
    object_type = _git(repo, "cat-file", "-t", oid).decode().strip()
    if object_type != "blob":
        raise ExportError(f"committed public-tree policy is not a blob: {relative_path}")
    if int(_git(repo, "cat-file", "-s", oid)) > _MAX_POLICY_BYTES:
        raise ExportError("committed public-tree policy exceeds its size bound")
    committed_bytes = _git(repo, "cat-file", "blob", oid)
    try:
        working_content = read_text_snapshot(path, max_bytes=_MAX_POLICY_BYTES).content
    except (OSError, ValueError) as error:
        raise ExportError(f"cannot read public-tree policy {path}: {error}") from error
    if working_content.encode("utf-8") != committed_bytes:
        raise ExportError(f"public-tree policy differs from committed candidate: {relative_path}")
    policy = _parse_policy(_json_object(working_content, "committed public-tree policy"))
    return policy, relative_path, oid, hashlib.sha256(committed_bytes).hexdigest()


def _require_absent(parent_fd: int, name: str, subject: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ExportError(f"{subject} must not exist: {name}")


def _require_directory_binding(parent_fd: int, name: str, descriptor: int) -> None:
    expected = os.fstat(descriptor)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise ExportError("export directory identity changed or was substituted")


def _require_parent_binding(path: Path, descriptor: int) -> None:
    current_fd = release_approval_archive._open_real_directory(path, create=False)
    try:
        expected = os.fstat(descriptor)
        current = os.fstat(current_fd)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ExportError("export parent directory identity changed")
    finally:
        os.close(current_fd)


def _write_file_at(parent_fd: int, name: str, content: bytes, mode: int) -> os.stat_result:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        os.fchmod(stream.fileno(), mode)
        stream.flush()
        os.fsync(stream.fileno())
        return os.fstat(stream.fileno())


def _read_file_at(parent_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExportError("export member is not an exclusive regular file")
        if metadata.st_size > quality_source.GIT_OUTPUT_LIMIT_BYTES:
            raise ExportError("export member exceeds its byte bound")
        content = stream.read(quality_source.GIT_OUTPUT_LIMIT_BYTES + 1)
        if len(content) > quality_source.GIT_OUTPUT_LIMIT_BYTES:
            raise ExportError("export member exceeds its byte bound")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ExportError("export member identity changed")
        return content, metadata


def _verify_tree_at(root_fd: int, entries: tuple[TreeEntry, ...]) -> None:
    expected = {entry.path: entry for entry in entries}
    expected_directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory_fd: int, prefix: str) -> None:
        for name in os.listdir(directory_fd):
            path = f"{prefix}/{name}" if prefix else name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(path)
                child_fd = release_approval_archive._open_directory_at(directory_fd, name)
                try:
                    visit(child_fd, path)
                    _require_directory_binding(directory_fd, name, child_fd)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode) and path in expected:
                content, metadata = _read_file_at(directory_fd, name)
                entry = expected[path]
                mode = 0o755 if entry.mode == "100755" else 0o644
                if stat.S_IMODE(metadata.st_mode) != mode or _git_object_id("blob", content) != entry.oid:
                    raise ExportError(f"export member bytes or mode changed: {path}")
                files.add(path)
            else:
                raise ExportError(f"unsupported export member: {path}")

    visit(root_fd, "")
    if files != set(expected) or directories != expected_directories:
        raise ExportError("export member inventory changed")


def _require_manifest_binding(path: Path, parent_fd: int, descriptor: int, content: bytes) -> None:
    _require_parent_binding(path.parent, parent_fd)
    metadata = os.fstat(descriptor)
    current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or metadata.st_nlink != 1
        or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        or os.pread(descriptor, len(content) + 1, 0) != content
    ):
        raise ExportError("export manifest identity or bytes changed")
    _require_parent_binding(path.parent, parent_fd)


def _write_manifest(path: Path, manifest: ExportManifest, *, parent_fd: int) -> tuple[int, bytes]:
    content = (json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name = f".{path.name}-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o600)
            stream.flush()
            os.fsync(stream.fileno())
        _require_parent_binding(path.parent, parent_fd)
        release_approval_archive._rename_no_replace_at(parent_fd, temporary_name, path.name)
        _require_manifest_binding(path, parent_fd, descriptor, content)
        return descriptor, content
    except BaseException:
        os.close(descriptor)
        raise


def _load_manifest(path: Path) -> ExportManifest:
    raw = _load_json(path)
    _validate_schema(raw, _MANIFEST_SCHEMA_PATH, "export manifest")
    expected = {
        "schema_version",
        "source_commit",
        "source_tree",
        "policy_path",
        "policy_oid",
        "policy_sha256",
        "expected_repository",
        "planned_tag",
        "exported_tree",
        "included",
        "excluded",
    }
    if set(raw) != expected or raw.get("schema_version") != 1:
        raise ExportError("export manifest has unsupported fields or schema version")

    def entries(field: str) -> tuple[TreeEntry, ...]:
        values = raw.get(field)
        if not isinstance(values, list):
            raise ExportError(f"export manifest {field} must be a list")
        parsed: list[TreeEntry] = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {"path", "mode", "oid", "category", "rule_id"}:
                raise ExportError(f"export manifest {field} contains an invalid entry")
            if not all(isinstance(value.get(key), str) for key in value):
                raise ExportError(f"export manifest {field} entry values must be strings")
            parsed.append(
                TreeEntry(
                    path=str(value["path"]),
                    mode=str(value["mode"]),
                    oid=str(value["oid"]),
                    category=str(value["category"]),
                    rule_id=str(value["rule_id"]),
                )
            )
        return tuple(parsed)

    scalar_fields = (
        "source_commit",
        "source_tree",
        "policy_path",
        "policy_oid",
        "policy_sha256",
        "expected_repository",
        "planned_tag",
        "exported_tree",
    )
    if not all(isinstance(raw.get(field), str) for field in scalar_fields):
        raise ExportError("export manifest identity fields must be strings")
    return ExportManifest(
        schema_version=1,
        source_commit=str(raw["source_commit"]),
        source_tree=str(raw["source_tree"]),
        policy_path=str(raw["policy_path"]),
        policy_oid=str(raw["policy_oid"]),
        policy_sha256=str(raw["policy_sha256"]),
        expected_repository=str(raw["expected_repository"]),
        planned_tag=str(raw["planned_tag"]),
        exported_tree=str(raw["exported_tree"]),
        included=entries("included"),
        excluded=entries("excluded"),
    )


def _validate_destinations(destination: Path, manifest_path: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ExportError(f"destination must not exist: {destination}")
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ExportError(f"manifest must not exist: {manifest_path}")
    if manifest_path.resolve().is_relative_to(destination.resolve()):
        raise ExportError("manifest must live outside the exported tree")


def export_tree(repo: Path, revision: str, policy_path: Path, destination: Path, manifest_path: Path) -> ExportManifest:
    """Materialize one policy-classified tree from an explicit committed revision."""
    _validate_destinations(destination, manifest_path)
    commit, source_tree = _resolve_commit(repo, revision)
    policy, policy_relative_path, policy_oid, policy_sha256 = _load_committed_policy(repo, commit, policy_path)
    included, excluded = _classify(_source_entries(repo, commit), policy)
    exported_tree = _tree_oid(included)
    manifest = ExportManifest(
        schema_version=1,
        source_commit=commit,
        source_tree=source_tree,
        policy_path=policy_relative_path,
        policy_oid=policy_oid,
        policy_sha256=policy_sha256,
        expected_repository=policy.expected_repository,
        planned_tag=policy.planned_tag,
        exported_tree=exported_tree,
        included=included,
        excluded=excluded,
    )
    manifest_data: dict[str, object] = json.loads(json.dumps(asdict(manifest)))
    _validate_schema(manifest_data, _MANIFEST_SCHEMA_PATH, "generated export manifest")
    try:
        with ExitStack() as descriptors:
            parent_fd = release_approval_archive._open_real_directory(destination.parent, create=True)
            descriptors.callback(os.close, parent_fd)
            manifest_parent_fd = release_approval_archive._open_real_directory(manifest_path.parent, create=True)
            descriptors.callback(os.close, manifest_parent_fd)
            _require_absent(parent_fd, destination.name, "destination")
            _require_absent(manifest_parent_fd, manifest_path.name, "manifest")
            staging_name, staging_fd = release_approval_archive._create_staging_directory(parent_fd, destination.name)
            descriptors.callback(os.close, staging_fd)
            for entry in included:
                relative = release_approval_archive._member_path(entry.path)
                directory_fd = release_approval_archive._open_or_create_member_directory(
                    staging_fd, relative.parts[:-1]
                )
                try:
                    _write_file_at(
                        directory_fd,
                        relative.parts[-1],
                        _git(repo, "cat-file", "blob", entry.oid),
                        0o755 if entry.mode == "100755" else 0o644,
                    )
                finally:
                    os.close(directory_fd)
            _verify_tree_at(staging_fd, included)
            _require_directory_binding(parent_fd, staging_name, staging_fd)
            _require_parent_binding(destination.parent, parent_fd)
            release_approval_archive._rename_no_replace_at(parent_fd, staging_name, destination.name)
            _require_directory_binding(parent_fd, destination.name, staging_fd)
            _verify_tree_at(staging_fd, included)
            manifest_fd, manifest_bytes = _write_manifest(manifest_path, manifest, parent_fd=manifest_parent_fd)
            descriptors.callback(os.close, manifest_fd)
            _require_directory_binding(parent_fd, destination.name, staging_fd)
            _verify_tree_at(staging_fd, included)
            _require_parent_binding(destination.parent, parent_fd)
            _require_manifest_binding(manifest_path, manifest_parent_fd, manifest_fd, manifest_bytes)
    except (OSError, ValueError) as error:
        raise ExportError(f"export publication failed; retained output requires inspection: {error}") from error
    return manifest


def _materialized_entries(destination: Path, manifest: ExportManifest) -> tuple[TreeEntry, ...]:
    if (destination / ".git").exists() or (destination / ".git").is_symlink():
        raise ExportError("export contains forbidden Git repository state")
    expected = {entry.path: entry for entry in manifest.included}
    expected_directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    actual_paths: set[str] = set()
    actual_directories: set[str] = set()
    for candidate in destination.rglob("*"):
        path = candidate.relative_to(destination).as_posix()
        filesystem_mode = candidate.lstat().st_mode
        if stat.S_ISREG(filesystem_mode) or stat.S_ISLNK(filesystem_mode):
            actual_paths.add(path)
        elif stat.S_ISDIR(filesystem_mode):
            actual_directories.add(path)
        else:
            raise ExportError(f"unsupported filesystem entry: {path}")
    if ".git" in actual_paths or any(path.startswith(".git/") for path in actual_paths):
        raise ExportError("export contains forbidden Git repository state")
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)
        extra = sorted(actual_paths - set(expected))
        raise ExportError(f"export path mismatch: missing={missing}, extra={extra}")
    if actual_directories != expected_directories:
        missing = sorted(expected_directories - actual_directories)
        extra = sorted(actual_directories - expected_directories)
        raise ExportError(f"export directory mismatch: missing={missing}, extra={extra}")
    verified: list[TreeEntry] = []
    for path in sorted(actual_paths):
        target = destination / path
        if target.is_symlink():
            raise ExportError(f"unsupported materialized symlink: {path}")
        parent_fd = release_approval_archive._open_real_directory(target.parent, create=False)
        try:
            content, metadata = _read_file_at(parent_fd, target.name)
            _require_parent_binding(target.parent, parent_fd)
        finally:
            os.close(parent_fd)
        mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        expected_entry = expected[path]
        if mode != expected_entry.mode:
            raise ExportError(f"mode mismatch for {path}: expected {expected_entry.mode}, got {mode}")
        oid = _git_object_id("blob", content)
        if oid != expected_entry.oid:
            raise ExportError(f"object mismatch for {path}: expected {expected_entry.oid}, got {oid}")
        verified.append(
            TreeEntry(
                path=path,
                mode=mode,
                oid=oid,
                category=expected_entry.category,
                rule_id=expected_entry.rule_id,
            )
        )
    return tuple(verified)


def verify_export(repo: Path, destination: Path, manifest_path: Path, policy_path: Path) -> ExportManifest:
    """Independently reconstruct and verify the source, policy, entries, and export tree."""
    if not destination.is_dir() or destination.is_symlink():
        raise ExportError(f"export destination is not a directory: {destination}")
    manifest = _load_manifest(manifest_path)
    commit, source_tree = _resolve_commit(repo, manifest.source_commit)
    if (commit, source_tree) != (manifest.source_commit, manifest.source_tree):
        raise ExportError("source commit or tree mismatch")
    policy, policy_relative_path, policy_oid, policy_sha256 = _load_committed_policy(repo, commit, policy_path)
    if (manifest.policy_path, manifest.policy_oid, manifest.policy_sha256) != (
        policy_relative_path,
        policy_oid,
        policy_sha256,
    ):
        raise ExportError("manifest policy identity mismatch")
    if (manifest.expected_repository, manifest.planned_tag) != (policy.expected_repository, policy.planned_tag):
        raise ExportError("manifest repository or planned tag mismatch")
    included, excluded = _classify(_source_entries(repo, commit), policy)
    if included != manifest.included or excluded != manifest.excluded:
        raise ExportError("manifest classification does not match the committed source tree")
    materialized = _materialized_entries(destination, manifest)
    tree = _tree_oid(materialized)
    if tree != manifest.exported_tree:
        raise ExportError(f"exported tree mismatch: expected {manifest.exported_tree}, got {tree}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Export or verify a clean public tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--policy", type=Path, default=_POLICY_PATH)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--revision", required=True)
    export_parser.add_argument("--destination", type=Path, required=True)
    export_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--destination", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    policy_path = args.policy if args.policy.is_absolute() else args.repo_root / args.policy
    try:
        if args.operation == "export":
            manifest = export_tree(args.repo_root, args.revision, policy_path, args.destination, args.manifest)
            print(
                f"Public tree export: PASS ({len(manifest.included)} included, "
                f"{len(manifest.excluded)} excluded, tree {manifest.exported_tree})"
            )
        else:
            manifest = verify_export(args.repo_root, args.destination, args.manifest, policy_path)
            print(f"Public tree verification: PASS (tree {manifest.exported_tree})")
    except (ExportError, OSError) as error:
        print(f"Public tree: ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
