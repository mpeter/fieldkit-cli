#!/usr/bin/env python3
"""PreToolUse hook: blocks writes of hyphenated sf- keys in pursuit frontmatter.

Reads Claude Code tool invocation JSON from stdin.
Exit 0 = allow, exit 2 = block with error message.
"""

import re
import sys
from fnmatch import fnmatch
from pathlib import Path

# Claude Code invokes this as `python3 hooks/pursuit_frontmatter_guard.py` (a
# direct script run, not `python3 -m hooks...`), so sys.path[0] is hooks/
# itself, not its parent -- the `hooks` package is unresolvable without this.
# Same pattern already used by hooks/pii_guard.py for its fieldkit import.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hooks._common import read_payload, tool_input, tool_name  # noqa: E402


def extract_frontmatter(content: str) -> str:
    """Extract the YAML frontmatter block (between first two --- delimiters)."""
    lines = content.splitlines()
    in_frontmatter = False
    frontmatter_lines: list[str] = []
    delimiter_count = 0

    for line in lines:
        if line.strip() == "---":
            delimiter_count += 1
            if delimiter_count == 1:
                in_frontmatter = True
                continue
            elif delimiter_count == 2:
                break
        elif in_frontmatter:
            frontmatter_lines.append(line)

    return "\n".join(frontmatter_lines)


def main() -> int:
    payload = read_payload()

    name = tool_name(payload)
    if name not in ("Write", "Edit", "MultiEdit"):
        return 0

    inputs = tool_input(payload)
    file_path = inputs.get("file_path", "")

    # Only pursuit files
    if not fnmatch(file_path, "accounts/*/pursuits/*.md"):
        return 0

    # Collect content based on tool type
    content = ""
    if name == "Write":
        content = inputs.get("content", "")
    elif name == "Edit":
        content = inputs.get("new_string", "")
    elif name == "MultiEdit":
        edits = inputs.get("edits", [])
        content = "\n".join(e.get("new_string", "") for e in edits)

    if not content:
        return 0

    frontmatter = extract_frontmatter(content)
    if not frontmatter:
        return 0

    # Find offending hyphenated sf- keys
    offending = sorted({re.sub(r":.*", "", line) for line in frontmatter.splitlines() if re.match(r"^sf-[a-z]", line)})

    if not offending:
        return 0

    # Build error message with underscore equivalents
    print("BLOCKED: Hyphenated sf- keys are not valid in pursuit frontmatter.", file=sys.stderr)
    print("Use underscores. Offending keys and their correct equivalents:", file=sys.stderr)
    for key in offending:
        fixed = key.replace("-", "_")
        print(f"  {key}  →  {fixed}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"File: {file_path}", file=sys.stderr)

    return 2


if __name__ == "__main__":
    sys.exit(main())
