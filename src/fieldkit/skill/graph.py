"""fieldkit.skill.graph -- Skill reference graph utilities.

Provides functions to extract Related Skills references from SKILL.md files
and to build an adjacency dict representing the skill reference graph.

Import constraint: MUST NOT import from ``fieldkit/`` or ``hooks/``.
Only stdlib (``re``, ``pathlib``) is used here -- no third-party dependencies.
Enforced by tach (fieldkit.skill depends only on declared foundation modules).
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Bold format:          - **skill-name** -- description
_R001_BOLD = re.compile(r"^\s*-\s+\*\*([a-z][a-z0-9-]+)\*\*")

# Backtick-slash format: - `/skill-name` -- description
_R001_BACKTICK = re.compile(r"^\s*-\s+`/([a-z][a-z0-9-]+)`")

# Matches the ## Related Skills section heading
_RS_HEADING = re.compile(r"^## Related Skills\s*$", re.MULTILINE)


def _split_related_skills_section(text: str) -> str:
    """Return the text of the ## Related Skills section, or empty string."""
    m = _RS_HEADING.search(text)
    if not m:
        return ""
    section_start = m.start()
    next_heading = re.search(r"^## ", text[m.end() :], re.MULTILINE)
    section_end = m.end() + next_heading.start() if next_heading else len(text)
    return text[section_start:section_end]


def extract_related_skill_names(text: str) -> list[str]:
    """Extract skill names from the ## Related Skills section of a SKILL.md file.

    Handles two entry formats:

    - Bold: ``- **skill-name** -- description``
    - Backtick-slash: ``- `/skill-name` -- description``

    Args:
        text: Full UTF-8 decoded content of a SKILL.md file.

    Returns:
        List of skill name strings extracted from the Related Skills section.
        Returns an empty list if the section is absent or contains no
        recognised entries.
    """
    related_section = _split_related_skills_section(text)
    if not related_section:
        return []
    names: list[str] = []
    for line in related_section.splitlines():
        m_bold = _R001_BOLD.match(line)
        if m_bold:
            names.append(m_bold.group(1))
            continue
        m_bt = _R001_BACKTICK.match(line)
        if m_bt:
            names.append(m_bt.group(1))
    return names


def build_related_graph(skills_dir: Path) -> dict[str, set[str]]:
    """Build adjacency dict: skill_name -> set of related skill names.

    Args:
        skills_dir: Root directory containing one sub-directory per skill.

    Returns:
        Adjacency dict. Returns an empty dict if ``skills_dir`` does not exist.
    """
    if not skills_dir.is_dir():
        return {}
    corpus: dict[str, Path] = {}
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            corpus[entry.name] = skill_md
    adjacency: dict[str, set[str]] = {name: set() for name in corpus}
    for skill_name, skill_md in corpus.items():
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for ref in extract_related_skill_names(text):
            if ref != skill_name and ref in corpus:
                adjacency[skill_name].add(ref)
    return adjacency
