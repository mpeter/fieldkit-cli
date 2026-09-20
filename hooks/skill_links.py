#!/usr/bin/env python3
"""Idempotent skill-symlink linker/checker for the operator's agent-skill surfaces.

Two symlink scopes are managed:

  1. Global personal skills — physical home ``~/.agents/skills/<name>``,
     consumed via ``~/.claude/skills/<name>`` which must be a RELATIVE
     symlink to ``../../.agents/skills/<name>``.
  2. Repo dev skills — physical home ``<repo>/.opencode/skills/<name>``,
     consumed via ``<repo>/.claude/skills`` (ideally one relative directory
     symlink ``.claude/skills -> ../.opencode/skills``; per-skill relative
     links are also acceptable).

Invariants enforced by ``check``:
  - No symlink under the global consumption dir may resolve into any git
    repository ("leak link" — an ancestor of the resolved target contains
    ``.git``).
  - No absolute symlinks.
  - No dangling links.
  - No name collisions: a skill name must exist in exactly one scope
    (global physical, global link target, repo).

``apply`` creates/repairs links, pruning dangling links and converting
absolute links that point into the global physical dir to relative ones.
It never deletes a leak link and never resolves a collision — both are
reported for manual (destructive) action by the operator.

Exit 1 = violations found (check) / manual-action items remain (apply).
Exit 0 = clean.

If ``--global-skills`` and ``--agents-skills`` resolve to the same directory
(e.g. ``~/.claude/skills`` is itself a whole-directory symlink to
``~/.agents/skills``, rather than containing per-entry links), the global
scope is treated as vacuously satisfied: both ``check`` and ``apply`` skip
per-entry scanning of it. Content found there is agents_skills' own physical
content — including any third-party symlinks it happens to contain — not a
per-entry consumption link this tool manages; scanning it for repair would
otherwise compute a relative target between two paths that are actually one,
producing a self-referential symlink (ELOOP).
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    kind: str  # "leak" | "absolute" | "dangling" | "collision"
    path: Path
    target: str


@dataclass(frozen=True)
class LinkAction:
    kind: str  # "create" | "repair" | "prune"
    path: Path
    target: str


@dataclass(frozen=True)
class ScanResult:
    violations: list[Violation]
    actions: list[LinkAction]


def _is_git_repo_ancestor(path: Path, *, consumer_root: Path) -> bool:
    """Return True if `path` or an ancestor has usable Git worktree metadata."""
    consumer_root = consumer_root.resolve()
    for ancestor in (path, *path.parents):
        if consumer_root.is_relative_to(ancestor):
            continue
        marker = ancestor / ".git"
        if marker.is_dir() and (marker / "HEAD").is_file() and (marker / "objects").is_dir():
            return True
        if marker.is_file():
            try:
                gitdir_line = marker.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, UnicodeDecodeError, IndexError):
                continue
            gitdir = gitdir_line.removeprefix("gitdir: ").strip()
            if (
                gitdir_line.startswith("gitdir: ")
                and gitdir
                and (ancestor / gitdir).is_dir()
                and (ancestor / gitdir / "HEAD").is_file()
            ):
                return True
    return False


def _is_skill_dir(entry: Path) -> bool:
    """A linkable skill entry: a real directory, not hidden/archived (dot-prefixed)."""
    return entry.is_dir() and not entry.name.startswith(".")


def _same_root(global_skills: Path, agents_skills: Path) -> bool:
    """True if global_skills IS agents_skills (e.g. a whole-directory symlink),
    not two distinct directories linked per-entry."""
    return global_skills.is_dir() and agents_skills.is_dir() and global_skills.resolve() == agents_skills.resolve()


def _iter_symlinks(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_symlink())


def check(global_skills: Path, agents_skills: Path, repo: Path) -> list[Violation]:
    """Scan the managed scopes and return every invariant violation found."""
    violations: list[Violation] = []
    seen_names: dict[str, list[str]] = {}

    def record_name(name: str, scope: str) -> None:
        seen_names.setdefault(name, []).append(scope)

    for phys_dir, scope in ((agents_skills, "global-physical"), (repo / ".opencode" / "skills", "repo")):
        if phys_dir.is_dir():
            for entry in sorted(phys_dir.iterdir()):
                if _is_skill_dir(entry):
                    record_name(entry.name, scope)

    # If the two roots are the same directory, everything "inside" is agents_skills'
    # own physical content (possibly third-party symlinks, e.g. from goose), not a
    # per-entry consumption link this tool manages — see _plan_actions for why.
    for link in [] if _same_root(global_skills, agents_skills) else _iter_symlinks(global_skills):
        raw_target = link.readlink()
        target_str = str(raw_target)

        if raw_target.is_absolute():
            violations.append(Violation(kind="absolute", path=link, target=target_str))

        resolved = link.resolve()
        if not resolved.exists():
            violations.append(Violation(kind="dangling", path=link, target=target_str))
            continue

        if _is_git_repo_ancestor(resolved, consumer_root=global_skills):
            violations.append(Violation(kind="leak", path=link, target=target_str))

        # A link that resolves to the expected agents_skills entry is just the
        # consumption view of the "global-physical" scope, not an independent
        # origin — only record it as its own scope when it points elsewhere
        # (a stray/misconfigured link), which is what can actually collide.
        expected = agents_skills / link.name
        if not (expected.exists() and resolved == expected.resolve()):
            record_name(link.name, "global-link")

    for name, scopes in sorted(seen_names.items()):
        distinct = sorted(set(scopes))
        if len(distinct) > 1:
            violations.append(Violation(kind="collision", path=Path(name), target=",".join(distinct)))

    return violations


def _plan_actions(
    global_skills: Path, agents_skills: Path, *, create_missing: bool = False
) -> tuple[list[LinkAction], list[Violation]]:
    """Plan create/repair/prune actions for the global scope only.

    Leak links and collisions are never auto-resolved; they are surfaced as
    violations for the caller to report and act on manually. New links for
    unlinked physical skills are created only with ``create_missing`` — the
    global consumption dir is operator-curated, so absence of a link is a
    choice (e.g. archived corpora), not drift.
    """
    actions: list[LinkAction] = []
    manual: list[Violation] = []

    if _same_root(global_skills, agents_skills):
        # The two scopes are already unified by definition. Scanning for repair
        # would compute relative targets between two paths that are actually one,
        # producing a self-referential symlink (ELOOP).
        return actions, manual

    physical_names = {p.name for p in agents_skills.iterdir() if _is_skill_dir(p)} if agents_skills.is_dir() else set()
    existing_links = {link.name: link for link in _iter_symlinks(global_skills)}

    def relative_target(name: str) -> str:
        return os.path.relpath(agents_skills / name, start=global_skills)

    for link_name, link in existing_links.items():
        raw_target = link.readlink()
        resolved = link.resolve()

        if not resolved.exists():
            actions.append(LinkAction(kind="prune", path=link, target=str(raw_target)))
            continue

        if _is_git_repo_ancestor(resolved, consumer_root=global_skills):
            manual.append(Violation(kind="leak", path=link, target=str(raw_target)))
            continue

        if link_name in physical_names and raw_target.is_absolute():
            # A genuinely cyclic agents_skills/link_name (e.g. one that resolves
            # back through global_skills/link_name itself) can never reach here:
            # _is_skill_dir's is_dir() check silently returns False for a symlink
            # it cannot resolve, so physical_names never contains such an entry —
            # verified empirically (Path.is_dir() on a cyclic symlink returns False,
            # it does not raise). No further self-loop check is needed here; the
            # _same_root guard above handles the one real case where this branch
            # would otherwise compute a self-referential relative path.
            actions.append(LinkAction(kind="repair", path=link, target=relative_target(link_name)))

    if create_missing:
        for name in sorted(physical_names):
            if name not in existing_links and not (global_skills / name).exists():
                actions.append(LinkAction(kind="create", path=global_skills / name, target=relative_target(name)))

    return actions, manual


def apply(
    global_skills: Path,
    agents_skills: Path,
    repo: Path,
    *,
    dry_run: bool = False,
    create_missing: bool = False,
) -> ScanResult:
    """Repair/prune links per the scope table; never touches leak links or collisions."""
    actions, manual_violations = _plan_actions(global_skills, agents_skills, create_missing=create_missing)

    if not dry_run:
        global_skills.mkdir(parents=True, exist_ok=True)
        for action in actions:
            if action.kind == "prune":
                action.path.unlink()
            else:
                if action.path.is_symlink() or action.path.exists():
                    action.path.unlink()
                action.path.symlink_to(Path(action.target), target_is_directory=True)

    collisions = [v for v in check(global_skills, agents_skills, repo) if v.kind == "collision"]
    return ScanResult(violations=manual_violations + collisions, actions=actions)


def _format_violation(v: Violation) -> str:
    return f"{v.kind}: {v.path} -> {v.target}"


def _format_action(a: LinkAction) -> str:
    return f"{a.kind}: {a.path} -> {a.target}"


def _default_repo() -> Path:
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage agent-skill symlink surfaces.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--global-skills", type=Path, default=Path.home() / ".claude" / "skills")
        sub.add_argument("--agents-skills", type=Path, default=Path.home() / ".agents" / "skills")
        sub.add_argument("--repo", type=Path, default=_default_repo())

    check_parser = subparsers.add_parser("check", help="Report violations without changing anything.")
    add_common_flags(check_parser)

    apply_parser = subparsers.add_parser("apply", help="Create/repair links; never resolves leaks or collisions.")
    add_common_flags(apply_parser)
    apply_parser.add_argument("--dry-run", action="store_true", help="Print planned actions without applying them.")
    apply_parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Also create links for unlinked ~/.agents skills (default: repair/prune only — absence is curation).",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        violations = check(args.global_skills, args.agents_skills, args.repo)
        if not violations:
            print("skill_links: clean")
            return 0
        print("skill_links: violations found", file=sys.stderr)
        for v in violations:
            print(f"  {_format_violation(v)}", file=sys.stderr)
        return 1

    result = apply(
        args.global_skills,
        args.agents_skills,
        args.repo,
        dry_run=args.dry_run,
        create_missing=args.create_missing,
    )
    label = "would " if args.dry_run else ""
    for action in result.actions:
        print(f"{label}{_format_action(action)}")
    if not result.violations:
        return 0
    print("skill_links: manual action required (not auto-resolved)", file=sys.stderr)
    for v in result.violations:
        print(f"  {_format_violation(v)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
