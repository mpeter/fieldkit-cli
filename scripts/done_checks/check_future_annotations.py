#!/usr/bin/env python3
"""Check the exact number of future-annotations imports in candidate files."""

from __future__ import annotations

import sys
from pathlib import Path

_IMPORT = "from __future__ import annotations"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_future_annotations.py EXPECTED_COUNT PATH...")
        return 1
    try:
        expected = int(argv[0])
    except ValueError:
        print(f"invalid expected count: {argv[0]!r}")
        return 1
    if expected < 0:
        print("expected count must be non-negative")
        return 1

    count = 0
    for raw_path in argv[1:]:
        path = Path(raw_path)
        try:
            count += sum(line.strip() == _IMPORT for line in path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError) as exc:
            print(f"cannot read {raw_path}: {exc}")
            return 1
    if count != expected:
        print(f"future annotations: expected {expected}, found {count}")
        return 1
    print(f"future annotations: found expected count {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
