"""Execution and outcome reporting for ``fieldkit skill install``."""

import json
from collections.abc import Callable
from itertools import product
from pathlib import Path
from typing import Literal

from fieldkit.commands.skill._install_io import _install_one_skill, _InstallDecision, _preflight_install
from fieldkit.commands.skill._output import InstallItemOutcome, json_enabled
from fieldkit.commands.skill._output import human_echo as _echo
from fieldkit.skill.targets import InstallTarget


def _install_to_tools(
    targets: list[InstallTarget],
    selected_skills: list[str],
    skills_dir: Path,
    ctx: dict[str, str],
    decisions: dict[tuple[str, str], _InstallDecision],
    *,
    dry_run: bool = False,
    outcomes: list[InstallItemOutcome] | None = None,
) -> tuple[int, int, int, int]:
    """Install skills to all selected tools. Returns (new, updated, unchanged, errors).

    Args:
        targets:         Validated install destinations.
        selected_skills: Skill names to install.
        skills_dir:      Source skills directory.
        ctx:             Template context for rendering.
        dry_run:         If True, print planned actions without writing.

    Returns:
        Tuple of ``(total_new, total_updated, total_unchanged, total_errors)``.
    """
    total_new = 0
    total_updated = 0
    total_unchanged = 0
    total_errors = 0

    for target_index, target in enumerate(targets):
        new, updated, unchanged, errors = _install_for_tool(
            target, selected_skills, skills_dir, ctx, decisions, dry_run=dry_run, outcomes=outcomes
        )
        total_new += new
        total_updated += updated
        total_unchanged += unchanged
        total_errors += errors
        if errors:
            if outcomes is not None:
                for remaining_target in targets[target_index + 1 :]:
                    outcomes.extend(
                        {"tool": remaining_target.key, "skill": skill, "status": "pending"} for skill in selected_skills
                    )
            break

    return total_new, total_updated, total_unchanged, total_errors


def _install_for_tool(
    target: InstallTarget,
    selected_skills: list[str],
    skills_dir: Path,
    ctx: dict[str, str],
    decisions: dict[tuple[str, str], _InstallDecision],
    *,
    dry_run: bool,
    outcomes: list[InstallItemOutcome] | None = None,
) -> tuple[int, int, int, int]:
    """Install one tool's skills and record ownership for successful writes."""
    from fieldkit.skill.install import installed_skill_digest, record_installed_skills
    from fieldkit.util.atomic import locked_json_update

    total_new = total_updated = total_unchanged = total_errors = 0
    installed_digests: dict[str, str] = {}
    _echo(f"Installing {len(selected_skills)} skills for {target.label}...")
    for skill_index, skill_name in enumerate(selected_skills):
        skill_dir = skills_dir / skill_name
        target_path = target.path_for(skill_name)
        decision = decisions[(target.key, skill_name)]
        new, updated, unchanged, errors = _install_one_skill(
            skill_name, skill_dir, target, ctx, decision, dry_run=dry_run
        )
        total_new += new
        total_updated += updated
        total_unchanged += unchanged
        total_errors += errors
        _record_install_outcome(
            outcomes,
            target=target,
            skill_name=skill_name,
            errors=errors,
            unchanged=unchanged,
            dry_run=dry_run,
        )
        if errors:
            if outcomes is not None:
                outcomes.extend(
                    {"tool": target.key, "skill": remaining, "status": "pending"}
                    for remaining in selected_skills[skill_index + 1 :]
                )
            break
        installed_digests[skill_name] = decision.expected_digest

    if installed_digests and not dry_run:
        try:
            for skill_name in installed_digests:
                target_path = target.path_for(skill_name)
                installed_digests[skill_name] = installed_skill_digest(target_path, target.format)
            record_installed_skills(target.manifest_path, installed_digests, locked_json_update)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _echo(f"  ✗ ownership manifest → {exc}", err=True)
            total_errors += 1
            if outcomes is not None:
                outcomes.append({"tool": target.key, "skill": "__ownership_manifest__", "status": "failed"})
    return total_new, total_updated, total_unchanged, total_errors


def _record_install_outcome(
    outcomes: list[InstallItemOutcome] | None,
    *,
    target: InstallTarget,
    skill_name: str,
    errors: int,
    unchanged: int,
    dry_run: bool,
) -> None:
    """Record the observable disposition of one attempted skill install."""
    if outcomes is None:
        return
    status: Literal["completed", "skipped", "failed", "pending"]
    if errors:
        status = "failed"
    elif unchanged:
        status = "skipped"
    elif dry_run:
        status = "pending"
    else:
        status = "completed"
    outcomes.append({"tool": target.key, "skill": skill_name, "status": status})


def _apply_install_plan(
    targets: list[InstallTarget],
    selected_skills: list[str],
    skills_dir: Path,
    *,
    dry_run: bool,
    prune: bool,
    confirm: bool,
    force: bool,
    build_template_ctx: Callable[[], dict[str, str]],
    outcomes: list[InstallItemOutcome] | None = None,
) -> tuple[int, int, int, int]:
    """Run selected installs, then prune only when those installs succeeded."""
    total_new = total_updated = total_unchanged = total_errors = 0
    if selected_skills:
        ctx = build_template_ctx()
        if not ctx:
            _echo("warning: config not found — skills installed without personalization", err=True)
        decisions, protected, total_errors = _preflight_install(targets, selected_skills, skills_dir, ctx)
        total_errors += _report_protected_skills(protected, force=force)
        if total_errors:
            _record_preflight_outcomes(
                outcomes,
                targets=targets,
                selected_skills=selected_skills,
                decisions=decisions,
                protected=protected,
                force=force,
            )
            if prune:
                _echo("Pruning skipped because installation preflight reported errors.", err=True)
            return 0, 0, 0, total_errors
        if outcomes is not None:
            total_new, total_updated, total_unchanged, total_errors = _install_to_tools(
                targets, selected_skills, skills_dir, ctx, decisions, dry_run=dry_run, outcomes=outcomes
            )
        else:
            total_new, total_updated, total_unchanged, total_errors = _install_to_tools(
                targets, selected_skills, skills_dir, ctx, decisions, dry_run=dry_run
            )
    if prune and total_errors:
        _echo("Pruning skipped because installation reported errors.", err=True)
    elif prune:
        total_errors += _prune_tools(targets, skills_dir, confirm=confirm, dry_run=dry_run, outcomes=outcomes)
    return total_new, total_updated, total_unchanged, total_errors


def _report_protected_skills(protected: list[str], *, force: bool) -> int:
    """Report protected targets and return their contribution to the error count."""
    if not protected or force:
        return 0
    _echo("Refusing to overwrite locally modified or untrusted skills:", err=True)
    for item in protected:
        _echo(f"  - {item}", err=True)
    _echo("Rerun with --force to overwrite these targets.", err=True)
    return len(protected)


def _record_preflight_outcomes(
    outcomes: list[InstallItemOutcome] | None,
    *,
    targets: list[InstallTarget],
    selected_skills: list[str],
    decisions: dict[tuple[str, str], _InstallDecision],
    protected: list[str],
    force: bool,
) -> None:
    """Record which preflight items failed and which remain retryable."""
    if outcomes is None:
        return
    protected_items = set(protected) if not force else set()
    for target, skill_name in product(targets, selected_skills):
        status = _preflight_outcome_status(target, skill_name, decisions, protected_items)
        outcomes.append({"tool": target.key, "skill": skill_name, "status": status})


def _preflight_outcome_status(
    target: InstallTarget,
    skill_name: str,
    decisions: dict[tuple[str, str], _InstallDecision],
    protected_items: set[str],
) -> Literal["failed", "pending"]:
    """Classify one item after a failed install preflight."""
    label = f"{target.label}: {skill_name}"
    if (target.key, skill_name) not in decisions or label in protected_items:
        return "failed"
    return "pending"


def _prune_tools(
    targets: list[InstallTarget],
    skills_dir: Path,
    *,
    confirm: bool,
    dry_run: bool,
    outcomes: list[InstallItemOutcome] | None = None,
) -> int:
    """Preview or remove stale installer-owned skills for selected tools."""
    from fieldkit.skill.install import discover_skill_names

    if not skills_dir.is_dir():
        _echo(f"Pruning skipped because the bundled skill directory is missing: {skills_dir}", err=True)
        return 1
    bundled_names = discover_skill_names(skills_dir)
    return sum(
        _prune_target(
            target,
            bundled_names,
            confirm=confirm,
            dry_run=dry_run,
            outcomes=outcomes,
        )
        for target in targets
    )


def _prune_target(
    target: InstallTarget,
    bundled_names: set[str],
    *,
    confirm: bool,
    dry_run: bool,
    outcomes: list[InstallItemOutcome] | None,
) -> int:
    """Preview or remove stale installer-owned skills for one tool."""
    from fieldkit.skill.install import inventory_prunable_skills, remove_owned_skills
    from fieldkit.util.atomic import locked_json_update

    try:
        inventory = inventory_prunable_skills(target, bundled_names)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _echo(f"  ✗ {target.label} prune inventory — {exc}", err=True)
        if outcomes is not None:
            outcomes.append({"tool": target.key, "skill": "__prune_inventory__", "status": "failed"})
        return 1

    _echo(f"{target.label}: {len(inventory.untracked)} untracked skill(s) left untouched.")
    if not inventory.stale_owned:
        _echo(f"{target.label}: no stale installer-owned skills.")
        return 0
    for name in inventory.stale_owned:
        _echo(f"  {'[preview]' if dry_run or not confirm else 'prune'} {name}")
    if dry_run or not confirm:
        if outcomes is not None:
            outcomes.extend({"tool": target.key, "skill": name, "status": "pending"} for name in inventory.stale_owned)
        if not dry_run:
            _echo("Preview only. Rerun with --prune --confirm to remove these skills.")
        return 0
    removed_before_failure: list[str] = []
    try:
        removed = remove_owned_skills(
            target,
            set(inventory.stale_owned),
            locked_json_update,
            on_remove=removed_before_failure.append,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _echo(f"  ✗ {target.label} prune — {exc}", err=True)
        _record_failed_prune(outcomes, target, inventory.stale_owned, removed_before_failure)
        return 1
    _echo(f"{target.label}: pruned {len(removed)} installer-owned skill(s).")
    if outcomes is not None:
        outcomes.extend({"tool": target.key, "skill": name, "status": "completed"} for name in removed)
    return 0


def _record_failed_prune(
    outcomes: list[InstallItemOutcome] | None,
    target: InstallTarget,
    stale_owned: list[str],
    removed: list[str],
) -> None:
    """Record the retry boundary after a prune failure."""
    if outcomes is None:
        return
    outcomes.extend({"tool": target.key, "skill": name, "status": "completed"} for name in removed)
    remaining = [name for name in stale_owned if name not in removed]
    if not remaining:
        outcomes.append({"tool": target.key, "skill": "__ownership_manifest__", "status": "failed"})
        return
    outcomes.append({"tool": target.key, "skill": remaining[0], "status": "failed"})
    outcomes.extend({"tool": target.key, "skill": name, "status": "pending"} for name in remaining[1:])


def _echo_install_summary(
    *,
    tool_count: int,
    total_new: int,
    total_updated: int,
    total_unchanged: int,
    total_errors: int,
    dry_run: bool = False,
    outcomes: list[InstallItemOutcome] | None = None,
) -> None:
    """Print the final count line, and the error line when anything failed."""
    total_installed = total_new + total_updated
    if json_enabled():
        grouped: dict[str, list[dict[str, str]]] = {
            "completed": [],
            "skipped": [],
            "failed": [],
            "pending": [],
        }
        for item in outcomes or []:
            grouped[item["status"]].append({"tool": item["tool"], "skill": item["skill"]})
        _echo(
            json.dumps(
                {
                    "errors": total_errors,
                    "dry_run": dry_run,
                    "installed": total_installed,
                    "new": total_new,
                    "tool_count": tool_count,
                    "unchanged": total_unchanged,
                    "updated": total_updated,
                    "outcomes": grouped,
                },
                sort_keys=True,
            ),
            force=True,
        )
        return
    _echo(
        f"\nDone. {total_installed} skills installed across {tool_count} tool(s)"
        f" ({total_new} new, {total_updated} updated, {total_unchanged} unchanged)."
        " Commit these files to version control."
    )
    if total_errors:
        _echo(f"{total_errors} error(s) occurred. See above for details.", err=True)
