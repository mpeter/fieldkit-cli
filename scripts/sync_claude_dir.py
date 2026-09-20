#!/usr/bin/env python3
"""sync_claude_dir.py — Keep .claude/ in sync with .opencode/ for Claude Code users.

.claude/ is the Claude Code runtime directory. It is kept as a mirror of
.opencode/agents/ and .opencode/commands/ per succession plan 1.4 (consolidate-not-
remove). This script copies any files present in .opencode/ but missing or outdated
in .claude/, and removes any .claude/ files that have no .opencode/ source (prune).

The mirror is not exhaustive: names in _CLAUDE_EXCLUDE are deliberately
OpenCode-only and are never copied. A stray .claude/ copy of an excluded name is
treated as an orphan and pruned.

Usage:
    uv run python scripts/sync_claude_dir.py [--check] [--prune]

Flags:
    --check   Dry-run: report what would be synced/pruned without making changes.
              Exits 1 if any files are out of sync or need pruning.
    --prune   Also remove .claude/{agents,commands}/*.md files that have no
              corresponding .opencode/ source. Without --prune, orphaned .claude/
              files are reported but not removed.

Exit codes:
    0 — in sync (or --check with no drift)
    1 — files synced/pruned (or --check with drift detected)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories to mirror: (source, dest)
_PAIRS: list[tuple[Path, Path]] = [
    (REPO_ROOT / ".opencode" / "agents", REPO_ROOT / ".claude" / "agents"),
    (REPO_ROOT / ".opencode" / "commands", REPO_ROOT / ".claude" / "commands"),
]

# Filenames never mirrored into .claude/. The proctor review pipeline routes
# through the proctor-* agent variants declared in .opencode/opencode.jsonc,
# which Claude Code cannot read — mirroring these would ship commands that
# dispatch to agents that do not resolve.
#
# The exclusion is transitive: assayer.md runs /proctor at its review step, so
# mirroring it would ship a command that fails partway through a PR it has
# already pushed commits to — a worse failure than not shipping it at all.
_CLAUDE_EXCLUDE: frozenset[str] = frozenset(
    {
        "assayer.md",
        "proctor.md",
        "review-pr.md",
        "review-council.md",
    }
)

_SKILLS_LINK = Path(".claude/skills")
_SKILLS_TARGET = Path("../.opencode/skills")


def _needs_sync(src: Path, dst: Path) -> bool:
    """Return True if dst is missing or has different content from src."""
    if not dst.exists():
        return True
    return src.read_bytes() != dst.read_bytes()


def _skills_link_error() -> str | None:
    """Return a diagnostic unless the Claude skills bridge is the canonical relative symlink."""
    link = REPO_ROOT / _SKILLS_LINK
    if link.is_symlink() and link.readlink() == _SKILLS_TARGET:
        return None
    return f"ERROR: {_SKILLS_LINK} must be a symlink to {_SKILLS_TARGET}; repair it manually"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: report drift without copying. Exits 1 if any files are out of sync.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove .claude/ files that have no .opencode/ source.",
    )
    args = parser.parse_args(argv)

    if not (REPO_ROOT / ".opencode").exists() and not (REPO_ROOT / ".claude").exists():
        print("Private agent adapter trees are not part of this repository; sync check skipped.")
        return 0

    if error := _skills_link_error():
        print(error, file=sys.stderr)
        return 1

    drifted: list[tuple[Path, Path]] = []
    orphaned: list[Path] = []

    for src_dir, dst_dir in _PAIRS:
        if not src_dir.is_dir():
            print(f"WARNING: source directory not found: {src_dir}", file=sys.stderr)
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)

        # Collect source filenames for prune check. Excluded names are omitted so
        # that any stray .claude/ copy of them is reported (and pruned) as an orphan.
        src_names = {
            f.name for f in src_dir.iterdir() if f.is_file() and f.suffix == ".md" and f.name not in _CLAUDE_EXCLUDE
        }

        # Check for files to sync (source → dest)
        for src_file in sorted(src_dir.iterdir()):
            if not src_file.is_file() or src_file.suffix != ".md":
                continue
            if src_file.name in _CLAUDE_EXCLUDE:
                continue
            dst_file = dst_dir / src_file.name
            if _needs_sync(src_file, dst_file):
                drifted.append((src_file, dst_file))

        # Check for orphaned dest files (dest has no source)
        for dst_file in sorted(dst_dir.iterdir()):
            if not dst_file.is_file() or dst_file.suffix != ".md":
                continue
            if dst_file.name not in src_names:
                orphaned.append(dst_file)

    any_drift = bool(drifted) or bool(orphaned)

    if not any_drift:
        print(".claude/ is in sync with .opencode/")
        return 0

    if args.check:
        if drifted:
            print(f".claude/ is OUT OF SYNC — {len(drifted)} file(s) differ:")
            for _src, dst in drifted:
                status = "MISSING" if not dst.exists() else "DIFFERS"
                print(f"  {status}  {dst.relative_to(REPO_ROOT)}")
        if orphaned:
            print(f".claude/ has {len(orphaned)} orphaned file(s) (no .opencode/ source):")
            for dst in orphaned:
                print(f"  ORPHAN  {dst.relative_to(REPO_ROOT)}")
            if not args.prune:
                print("  (run with --prune to remove orphaned files)")
        return 1

    # Sync: copy drifted files
    for src, dst in drifted:
        status = "added" if not dst.exists() else "updated"
        shutil.copy2(src, dst)
        print(f"  {status}  {dst.relative_to(REPO_ROOT)}")

    if drifted:
        print(f"\nSynced {len(drifted)} file(s) from .opencode/ to .claude/")

    # Prune: remove orphaned files
    if orphaned:
        if args.prune:
            for dst in orphaned:
                dst.unlink()
                print(f"  pruned  {dst.relative_to(REPO_ROOT)}")
            print(f"\nPruned {len(orphaned)} orphaned file(s) from .claude/")
        else:
            print(f"\nWARNING: {len(orphaned)} orphaned file(s) in .claude/ (run with --prune to remove):")
            for dst in orphaned:
                print(f"  {dst.relative_to(REPO_ROOT)}")

    return 1  # Return 1 so callers know changes were made


if __name__ == "__main__":
    sys.exit(main())
