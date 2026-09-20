#!/usr/bin/env python3
"""Check for broken relative links in memory/MEMORY.md.

Usage:
    python scripts/check_memory_links.py [--memory-dir PATH]

The fieldkit_home path is auto-detected from the fieldkit config when
--memory-dir is not provided.

Exit code: 0 always (non-blocking diagnostic tool — warnings only).
"""

import re
import sys
from pathlib import Path

# Matches Markdown inline links: [text](href)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def check_memory_links(memory_dir: Path) -> list[str]:
    """Return a list of broken relative link descriptions.

    Skips external links (http/https) and anchor-only links (#fragment).
    Resolves relative paths against memory_dir and checks existence.

    Args:
        memory_dir: Directory containing MEMORY.md.

    Returns:
        List of human-readable broken-link descriptions (empty when all OK).
    """
    memory_md = memory_dir / "MEMORY.md"
    if not memory_md.exists():
        return []

    content = memory_md.read_text(encoding="utf-8")
    broken: list[str] = []
    memory_dir_resolved = memory_dir.resolve()

    for match in _LINK_RE.finditer(content):
        text = match.group(1)
        href = match.group(2)

        # Skip external URLs, pure anchor links, and absolute filesystem paths.
        # These cannot be checked against the local filesystem or would allow
        # path traversal outside the memory directory.
        if href.startswith(("http://", "https://", "#", "/")):
            continue

        resolved = (memory_dir / href).resolve()

        # Guard against path traversal via "../" sequences that escape memory_dir.
        # A link like "../../etc/passwd" would resolve outside the workspace.
        if not str(resolved).startswith(str(memory_dir_resolved)):
            broken.append(f"TRAVERSAL: [{text}]({href}) → {resolved} (escapes memory dir)")
            continue

        if not resolved.exists():
            broken.append(f"BROKEN: [{text}]({href}) → {resolved}")

    return broken


def main() -> None:
    """CLI entry point — prints warnings to stderr, exits 0 always."""
    import argparse

    parser = argparse.ArgumentParser(description="Check MEMORY.md for broken relative links.")
    parser.add_argument("--memory-dir", type=Path, default=None, help="Path to the memory directory.")
    args = parser.parse_args()

    if args.memory_dir:
        memory_dir: Path = args.memory_dir
    else:
        try:
            from fieldkit.config import get_fieldkit_home

            memory_dir = get_fieldkit_home() / "memory"
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: cannot resolve fieldkit home: {exc}", file=sys.stderr)
            sys.exit(0)

    broken = check_memory_links(memory_dir)
    for msg in broken:
        print(f"WARNING: {msg}", file=sys.stderr)
    if not broken:
        print("OK: no broken links in MEMORY.md")


if __name__ == "__main__":
    main()
