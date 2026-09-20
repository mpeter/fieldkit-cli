"""Preflight and filesystem execution for project-local skill installation."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fieldkit.commands.skill._output import human_echo, json_enabled
from fieldkit.skill.targets import InstallTarget

if TYPE_CHECKING:
    from fieldkit.skill.template import SkillInstallResult


@dataclass(frozen=True)
class _InstallDecision:
    """Preflight classification for one tool and skill target."""

    status: Literal["new", "unchanged", "update", "protected"]
    expected_digest: str
    observed_digest: str | None


def _is_path_safe(target_path: Path, skill_root: Path, skill_name: str) -> bool:
    """Return True if target_path is safely inside its registered skill root.

    Guards against symlink-based path traversal attacks.
    """
    try:
        resolved = target_path.resolve()
        if not resolved.is_relative_to(skill_root):
            human_echo(
                f"  ✗ {skill_name}  — resolved path escapes registered skill root (rejected)",
                err=True,
            )
            return False
    except ValueError:
        human_echo(f"  ✗ {skill_name}  — path resolution failed", err=True)
        return False
    return True


def _install_one_skill(
    skill_name: str,
    skill_dir: Path,
    target: InstallTarget,
    ctx: dict[str, str],
    decision: _InstallDecision,
    *,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    """Install one preflighted target. Returns (new, updated, unchanged, errors)."""
    target_path = target.path_for(skill_name)
    if error := _prewrite_error(skill_name, skill_dir, target, decision.observed_digest):
        human_echo(f"  ✗ {skill_name}  → {error}", err=True)
        return 0, 0, 0, 1

    if decision.status == "unchanged":
        human_echo(f"  · {skill_name:<28} → unchanged")
        return 0, 0, 1, 0

    is_update = decision.status != "new"

    result = _write_skill(skill_dir, target_path, target.format, ctx, dry_run=dry_run)

    if result.errors:
        human_echo(f"  ✗ {skill_name}  → error", err=True)
        return 0, 0, 0, result.errors

    if cleanup_error := _cleanup_stale_files(skill_dir, target_path, target.format, dry_run=dry_run):
        human_echo(f"  ✗ {skill_name}  → {cleanup_error}", err=True)
        return 0, 0, 0, 1

    if is_update:
        suffix = " (forced local modifications overwritten)" if decision.status == "protected" else ""
        human_echo(f"  ✓ {skill_name:<28} → updated{suffix}")
        return 0, 1, 0, 0

    human_echo(f"  ✓ {skill_name:<28} → new")
    return 1, 0, 0, 0


def _prewrite_error(
    skill_name: str,
    skill_dir: Path,
    target: InstallTarget,
    observed_digest: str | None,
) -> str | None:
    """Return an actionable error when a preflighted target is no longer safe."""
    from fieldkit.skill.install import installed_state_matches

    if not skill_dir.is_dir():
        return "skill directory not found"
    target_path = target.path_for(skill_name)
    if not _is_path_safe(target_path, target.skill_root, skill_name):
        return "unsafe target path"
    try:
        if not installed_state_matches(target_path, target.format, observed_digest):
            return "target changed after preflight; refusing overwrite"
    except (OSError, ValueError) as exc:
        return f"pre-write validation failed: {exc}"
    return None


def _write_skill(
    skill_dir: Path, target_path: Path, target_format: str, ctx: dict[str, str], *, dry_run: bool
) -> "SkillInstallResult":
    """Render or copy one flat or directory-format skill."""
    from fieldkit.skill.template import install_skill_dir, install_skill_flat

    if target_format == "flat":
        return install_skill_flat(skill_dir, target_path, ctx, dry_run=dry_run, quiet=json_enabled())
    return install_skill_dir(skill_dir, target_path.parent, ctx, dry_run=dry_run, quiet=json_enabled())


def _cleanup_stale_files(skill_dir: Path, target_path: Path, target_format: str, *, dry_run: bool) -> str | None:
    """Reconcile stale directory entries after a successful write."""
    from fieldkit.skill.install import remove_stale_installed_files

    if target_format == "flat" or dry_run:
        return None
    try:
        remove_stale_installed_files(skill_dir, target_path.parent)
    except OSError as exc:
        return f"stale-file cleanup failed: {exc}"
    return None


def _preflight_install(
    targets: list[InstallTarget], selected_skills: list[str], skills_dir: Path, ctx: dict[str, str]
) -> tuple[dict[tuple[str, str], _InstallDecision], list[str], int]:
    """Classify the full install plan before any target is written."""
    from fieldkit.skill.install import (
        installed_skill_digest,
        load_ownership,
        rendered_skill_digest,
    )

    decisions: dict[tuple[str, str], _InstallDecision] = {}
    protected: list[str] = []
    errors = 0
    for target in targets:
        try:
            ownership = load_ownership(target.manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            human_echo(f"  ✗ {target.label} ownership manifest → {exc}", err=True)
            errors += 1
            continue
        for skill_name in selected_skills:
            skill_dir = skills_dir / skill_name
            target_path = target.path_for(skill_name)
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
                human_echo(f"  ✗ {skill_name}  — SKILL.md not found", err=True)
                errors += 1
                continue
            if not _is_path_safe(target_path, target.skill_root, skill_name):
                errors += 1
                continue
            try:
                expected = rendered_skill_digest(skill_dir, target.format, ctx)
                status: Literal["new", "unchanged", "update", "protected"]
                install_root = target_path if target.format == "flat" else target_path.parent
                observed: str | None = None
                if not install_root.exists() and not install_root.is_symlink():
                    status = "new"
                else:
                    current = installed_skill_digest(target_path, target.format)
                    observed = current
                    if current == expected:
                        status = "unchanged"
                    elif ownership.digests.get(skill_name) == current:
                        status = "update"
                    else:
                        status = "protected"
                        protected.append(f"{target.label}: {skill_name}")
                decisions[(target.key, skill_name)] = _InstallDecision(status, expected, observed)
            except (OSError, ValueError) as exc:
                human_echo(f"  ✗ {target.label}: {skill_name} preflight → {exc}", err=True)
                errors += 1
    return decisions, protected, errors
