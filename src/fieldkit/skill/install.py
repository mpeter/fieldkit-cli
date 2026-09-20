"""Selection and ownership helpers for ``fieldkit skill install``.

Interactive selection and user-facing output stay in
``commands/skill/_runner.py``. This module owns tool detection, installable-skill
discovery, and the locked manifests that authorize safe pruning.
"""

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fieldkit.skill.targets import SKILL_CATEGORIES, TOOL_TARGETS, InstallTarget

_MANIFEST_VERSION = 2
_LEGACY_MANIFEST_VERSION = 1
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
JsonUpdate = Callable[[Path], AbstractContextManager[dict[str, Any]]]


@dataclass(frozen=True)
class PruneInventory:
    """Owned stale and untracked skill names for one tool target."""

    stale_owned: list[str]
    untracked: list[str]


@dataclass(frozen=True)
class OwnershipState:
    """Validated ownership names and their last installed content digests."""

    owned: set[str]
    digests: dict[str, str]


def _parse_ownership(data: dict[str, Any]) -> OwnershipState:
    """Validate manifest data while retaining version-1 ownership."""
    if not data:
        return OwnershipState(set(), {})
    version = data.get("version")
    if version not in {_LEGACY_MANIFEST_VERSION, _MANIFEST_VERSION}:
        raise ValueError("unsupported skill install manifest version")
    owned = _parse_owned_names(data.get("skills"))
    if version == _LEGACY_MANIFEST_VERSION:
        return OwnershipState(owned, {})
    return OwnershipState(owned, _parse_manifest_digests(data.get("digests", {}), owned))


def _parse_owned_names(raw_skills: Any) -> set[str]:
    """Return validated installer-owned skill names."""
    if not isinstance(raw_skills, list) or not all(isinstance(name, str) for name in raw_skills):
        raise ValueError("invalid skill install manifest skills list")
    if invalid := sorted(name for name in raw_skills if not _SKILL_NAME_RE.fullmatch(name)):
        raise ValueError(f"invalid skill name in install manifest: {invalid[0]!r}")
    return set(raw_skills)


def _parse_manifest_digests(raw_digests: Any, owned: set[str]) -> dict[str, str]:
    """Return validated per-skill digests from a version-2 manifest."""
    if not isinstance(raw_digests, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in raw_digests.items()
    ):
        raise ValueError("invalid skill install manifest digests")
    _validate_digest_names(raw_digests, owned)
    _validate_digest_values(raw_digests)
    return dict(raw_digests)


def _validate_digest_names(digests: dict[str, str], owned: set[str]) -> None:
    """Reject digest entries outside the validated ownership set."""
    if invalid := sorted(name for name in digests if name not in owned or not _SKILL_NAME_RE.fullmatch(name)):
        raise ValueError(f"invalid digest skill name in install manifest: {invalid[0]!r}")


def _validate_digest_values(digests: dict[str, str]) -> None:
    """Reject malformed SHA-256 digest values."""
    if invalid_digest := next((digest for digest in digests.values() if not _DIGEST_RE.fullmatch(digest)), None):
        raise ValueError(f"invalid skill digest in install manifest: {invalid_digest!r}")


def _manifest_document(state: OwnershipState) -> dict[str, Any]:
    """Return the canonical version-2 manifest document."""
    return {"version": _MANIFEST_VERSION, "skills": sorted(state.owned), "digests": dict(sorted(state.digests.items()))}


def load_ownership(manifest_path: Path) -> OwnershipState:
    """Load and validate ownership state from *manifest_path*."""
    if not manifest_path.exists():
        return OwnershipState(set(), {})
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid skill install manifest document")
    return _parse_ownership(data)


def record_installed_skills(manifest_path: Path, skill_digests: dict[str, str], update_json: JsonUpdate) -> None:
    """Atomically record successfully installed skill digests."""
    if not skill_digests:
        return
    with update_json(manifest_path) as data:
        state = _parse_ownership(data)
        owned = state.owned | skill_digests.keys()
        digests = state.digests | skill_digests
        data.clear()
        data.update(_manifest_document(OwnershipState(owned, digests)))


def _digest_entries(entries: list[tuple[str, bytes]]) -> str:
    """Hash path-delimited content entries deterministically."""
    digest = hashlib.sha256()
    for relative_path, content in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def installed_skill_digest(target_path: Path, target_format: str) -> str:
    """Return a digest of an existing flat file or directory-format target."""
    root = target_path if target_format == "flat" else target_path.parent
    if target_format == "flat":
        if root.is_symlink() or not root.is_file():
            raise ValueError(f"installed skill target is not a regular file: {root}")
        return _digest_entries([("SKILL.md", root.read_bytes())])
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"installed skill target is not a regular directory: {root}")
    entries: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"installed skill contains a symlink: {path}")
        if path.is_file():
            entries.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return _digest_entries(entries)


def installed_state_matches(target_path: Path, target_format: str, observed_digest: str | None) -> bool:
    """Return whether a target still matches its preflight state."""
    install_root = target_path if target_format == "flat" else target_path.parent
    if observed_digest is None:
        return not install_root.exists() and not install_root.is_symlink()
    return installed_skill_digest(target_path, target_format) == observed_digest


def _rendered_flat_skill_digest(skill_dir: Path, ctx: dict[str, str]) -> str:
    """Return the digest of a rendered single-file skill artifact."""
    from fieldkit.skill.template import _render_flat_skill_bundle

    flat_content, _unresolved = _render_flat_skill_bundle(skill_dir, ctx)
    return _digest_entries([("SKILL.md", flat_content.encode("utf-8"))])


def _rendered_directory_skill_digest(skill_dir: Path, ctx: dict[str, str]) -> str:
    """Return the digest of a rendered directory-based skill artifact."""
    from fieldkit.skill.template import render_skill_text

    entries: list[tuple[str, bytes]] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        if path.suffix == ".md":
            content = render_skill_text(content.decode("utf-8"), ctx)[0].encode("utf-8")
        entries.append((path.relative_to(skill_dir).as_posix(), content))
    return _digest_entries(entries)


def rendered_skill_digest(skill_dir: Path, target_format: str, ctx: dict[str, str]) -> str:
    """Return the digest that installing *skill_dir* would produce."""
    if target_format == "flat":
        return _rendered_flat_skill_digest(skill_dir, ctx)
    return _rendered_directory_skill_digest(skill_dir, ctx)


def remove_stale_installed_files(skill_dir: Path, target_dir: Path) -> None:
    """Remove target entries that are absent from the bundled directory."""
    source_files = {path.relative_to(skill_dir) for path in skill_dir.rglob("*") if path.is_file()}
    for path in sorted(target_dir.rglob("*"), reverse=True):
        if path.is_symlink() or (path.is_file() and path.relative_to(target_dir) not in source_files):
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _installed_skill_names(target: InstallTarget) -> set[str]:
    """Return names visible in a tool's registered skill root."""
    root = target.skill_root
    if not root.is_dir():
        return set()
    if target.format == "flat":
        return {path.stem for path in root.glob("*.md") if not path.name.startswith(".")}
    return {
        path.name
        for path in root.iterdir()
        if not path.name.startswith(".") and path.is_dir() and (path / "SKILL.md").is_file()
    }


def inventory_prunable_skills(target: InstallTarget, bundled_names: set[str]) -> PruneInventory:
    """Inventory stale owned and untracked names without mutating the target."""
    owned = load_ownership(target.manifest_path).owned
    installed = _installed_skill_names(target)
    return PruneInventory(
        stale_owned=sorted(owned - bundled_names),
        untracked=sorted(installed - owned),
    )


def remove_owned_skills(
    target: InstallTarget,
    skill_names: set[str],
    update_json: JsonUpdate,
    *,
    on_remove: Callable[[str], None] | None = None,
) -> list[str]:
    """Remove validated owned targets and reconcile their manifest entries."""
    manifest_path = target.manifest_path
    removed: list[str] = []
    with update_json(manifest_path) as data:
        state = _parse_ownership(data)
        owned = state.owned
        for name in sorted(skill_names & owned):
            target_path = target.path_for(name)
            delete_path = target_path if target.format == "flat" else target_path.parent
            if not delete_path.resolve().is_relative_to(target.skill_root):
                raise ValueError(f"owned skill path escapes registered skill root: {name!r}")
            if delete_path.is_symlink() or delete_path.is_file():
                delete_path.unlink()
            elif delete_path.is_dir():
                shutil.rmtree(delete_path)
            removed.append(name)
            if on_remove is not None:
                on_remove(name)
        owned.difference_update(removed)
        digests = {name: digest for name, digest in state.digests.items() if name in owned}
        data.clear()
        data.update(_manifest_document(OwnershipState(owned, digests)))
    return removed


def expand_all_skills() -> list[str]:
    """Return every skill name in SKILL_CATEGORIES — the ``--all`` expansion.

    Order follows SKILL_CATEGORIES insertion order, so the resulting install
    summary is deterministic.
    """
    return [name for category_skills in SKILL_CATEGORIES.values() for name in category_skills]


def detect_tools(cwd: Path) -> list[str]:
    """Return the key of every tool whose marker directory exists under *cwd*.

    This is the pre-check state offered to the tool-selection prompt; it does
    not decide what gets installed.
    """
    return [key for key, target in TOOL_TARGETS.items() if (cwd / target.detect).is_dir()]


def discover_skill_names(skills_dir: Path) -> set[str]:
    """Return the name of every skill directory in *skills_dir*.

    An absent *skills_dir* yields an empty set rather than raising: the caller
    uses this only to widen related-skill suggestions, and having none is a
    normal state, not an error.
    """
    if not skills_dir.is_dir():
        return set()
    return {entry.name for entry in skills_dir.iterdir() if entry.is_dir()}
