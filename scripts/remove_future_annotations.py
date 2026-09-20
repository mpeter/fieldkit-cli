#!/usr/bin/env python3
"""Remove `from __future__ import annotations` from all Python files under given roots.

Usage:
    uv run python scripts/remove_future_annotations.py src/ tests/ hooks/

The script is idempotent: running it twice produces the same result.
It prints a summary of files modified and files already clean.
"""

import sys
from pathlib import Path

TARGET = "from __future__ import annotations"


def process_file(path: Path) -> bool:
    """Remove the target import from path. Return True if the file was modified."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    new_lines = [ln for ln in lines if ln.rstrip("\n\r") != TARGET]
    if len(new_lines) == len(lines):
        return False
    path.write_text("".join(new_lines), encoding="utf-8")
    return True


def main() -> None:
    roots = [Path(arg) for arg in sys.argv[1:]] if len(sys.argv) > 1 else [Path()]
    modified = 0
    clean = 0
    for root in roots:
        py_files = [root] if root.is_file() and root.suffix == ".py" else sorted(root.rglob("*.py"))
        for py_file in py_files:
            if process_file(py_file):
                print(f"  cleaned: {py_file}")
                modified += 1
            else:
                clean += 1
    print(f"\nDone: {modified} file(s) modified, {clean} already clean.")


if __name__ == "__main__":
    main()
