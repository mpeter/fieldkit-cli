#!/usr/bin/env python3
"""Fold changelog.d/ fragments into CHANGELOG.md under ``## [Unreleased]``.

Fragments are inserted in reverse-alphabetical filename order and separated by
``---``; the fragment files are then deleted. Ordering is lexicographic by slug,
NOT chronological — lexicographic numeric-looking slugs do not sort numerically.
Reorder by hand after assembling if the sequence matters for a release.

See changelog.d/README.md for the authoring format.

Usage:
    uv run python scripts/build_changelog.py            # assemble and delete fragments
    uv run python scripts/build_changelog.py --dry-run  # print result, change nothing

Exit 0: assembled, or nothing to do.
Exit 1: CHANGELOG.md missing the ``## [Unreleased]`` anchor, or fragment cleanup
        failed partway (surviving fragments are named on stderr).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _changelog_common import FRAGMENTS_DIRNAME, META_FRAGMENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
FRAGMENTS_DIR = REPO_ROOT / FRAGMENTS_DIRNAME

UNRELEASED_HEADING = "## [Unreleased]"
EMPTY_UNRELEASED_PLACEHOLDER = "No public changes yet."
SEPARATOR = "\n\n---\n\n"


def _collect_fragments() -> list[Path]:
    """Return real fragment paths in reverse-alphabetical filename order."""
    if not FRAGMENTS_DIR.is_dir():
        return []
    return sorted(
        (path for path in FRAGMENTS_DIR.glob("*.md") if path.name not in META_FRAGMENTS),
        reverse=True,
    )


def _heading_of(fragment_text: str) -> str | None:
    """Return the fragment's first ``###`` heading line, or None if it has none."""
    for line in fragment_text.splitlines():
        if line.startswith("### "):
            return line.strip()
    return None


def _assemble(changelog: str, fragments: list[Path]) -> str:
    """Return *changelog* with *fragments* inserted under the Unreleased heading.

    Fragments whose ``###`` heading already appears in *changelog* are skipped.
    Without that guard, re-running after an interrupted cleanup (CHANGELOG.md
    written, fragments only partly deleted) folds the survivors in a second time.

    Raises:
        ValueError: when the Unreleased heading is absent.
    """
    lines = changelog.split("\n")
    try:
        anchor = lines.index(UNRELEASED_HEADING)
    except ValueError as exc:
        raise ValueError(f"{UNRELEASED_HEADING!r} not found in CHANGELOG.md") from exc

    head = "\n".join(lines[: anchor + 1])
    tail = "\n".join(lines[anchor + 1 :]).strip()
    if fragments and tail.startswith(EMPTY_UNRELEASED_PLACEHOLDER):
        tail = tail.removeprefix(EMPTY_UNRELEASED_PLACEHOLDER).lstrip()

    blocks: list[str] = []
    for path in fragments:
        text = path.read_text(encoding="utf-8").strip()
        heading = _heading_of(text)
        if heading is not None and heading in changelog:
            print(f"Skipping {path.name}: {heading!r} already in CHANGELOG.md")
            continue
        blocks.append(text)

    if tail:
        blocks.append(tail)
    return f"{head}\n\n{SEPARATOR.join(blocks)}\n"


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* via a same-directory temp file and os-level rename.

    ``write_text`` truncates before writing, so an interruption mid-write leaves a
    truncated CHANGELOG.md. ``Path.replace`` is atomic within a filesystem.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _delete_fragments(fragments: list[Path]) -> list[Path]:
    """Delete *fragments*, returning any that could not be removed."""
    survivors: list[Path] = []
    for path in fragments:
        try:
            path.unlink()
        except OSError as exc:
            print(f"ERROR: could not delete {path.name}: {exc}", file=sys.stderr)
            survivors.append(path)
    return survivors


def main(argv: list[str] | None = None) -> int:
    """Assemble fragments into CHANGELOG.md.

    Returns:
        0 when assembled or when there was nothing to do.
        1 when CHANGELOG.md lacks the Unreleased heading, or cleanup left files behind.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the assembled CHANGELOG.md to stdout and change nothing",
    )
    # parse_args rejects unknown flags, so a `--dryrun` typo errors out instead of
    # silently falling through to the destructive path.
    args = parser.parse_args(argv)

    fragments = _collect_fragments()
    if not fragments:
        print("No changelog fragments to assemble.")
        return 0

    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    try:
        assembled = _assemble(changelog, fragments)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(assembled)
        return 0

    _write_atomic(CHANGELOG_PATH, assembled)
    survivors = _delete_fragments(fragments)

    consumed = [path for path in fragments if path not in survivors]
    print(f"Assembled {len(consumed)} fragment(s) into CHANGELOG.md:")
    for path in consumed:
        print(f"  {path.name}")

    if survivors:
        print("", file=sys.stderr)
        print("CHANGELOG.md was written, but these fragments survived and would be", file=sys.stderr)
        print("folded in again on a re-run — delete them by hand:", file=sys.stderr)
        for path in survivors:
            print(f"  {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
