"""Constants shared by the changelog gate and the changelog assembler.

One home for the fragment-directory vocabulary. When these lived in both
scripts, registering a new meta file in only one of them produced a silent
split-brain: the gate would accept a template edit as a real entry, or the
assembler would fold template placeholder text into CHANGELOG.md.
"""

import re

# Directory holding one markdown fragment per change, relative to the repo root.
FRAGMENTS_DIRNAME = "changelog.d"

# Files inside FRAGMENTS_DIRNAME that document the convention rather than
# describe a change. Adding or editing one neither satisfies the gate nor gets
# assembled into CHANGELOG.md.
META_FRAGMENTS: frozenset[str] = frozenset({"README.md", ".gitkeep"})


def fragment_boundary_problem(content: str) -> str | None:
    """Reserve structural delimiters so assembly retries cannot split an entry."""
    for line in content.splitlines():
        if re.match(r"^ {0,3}##(?:[ \t]|$)", line) or line.strip() == "---":
            return "hash-prefixed level-two headings and standalone --- lines are reserved for changelog assembly"
    return None
