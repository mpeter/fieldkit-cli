"""Reference graph validation for the skill-integrity corpus."""

from collections import deque
from pathlib import Path

from skill_integrity import corpus, model, references


def _extract_related_skill_names(text: str) -> list[str]:
    """Extract skill names from the ## Related Skills section of a file."""
    _, related_section, _ = references._split_body_and_related_skills(text)
    if not related_section:
        return []

    names: list[str] = []
    for line in related_section.splitlines():
        m_bold = references._R001_BOLD.match(line)
        if m_bold:
            names.append(m_bold.group(1))
            continue
        m_bt = references._R001_BACKTICK.match(line)
        if m_bt:
            names.append(m_bt.group(1))
    return names


def _extract_skills_use_names(text: str) -> list[str]:
    """Extract skill names from skills_use({name: "..."}) calls."""
    return references._R006_PATTERN.findall(text)


def check_dag(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Detect DAG violations: G001 (cycles) and G002 (orphans).

    Uses Kahn's algorithm for cycle detection and BFS from agents+commands
    for orphan detection.

    Args:
        registry: Populated corpus registry.

    Returns:
        List of DAG violations (cycles and orphans).
    """
    violations: list[model.Violation] = []
    all_skills = references._all_skill_names(registry)

    # Build adjacency list: skill_name -> set of referenced skill names
    # (only edges to skills that actually exist in the registry)
    adjacency: dict[str, set[str]] = {name: set() for name in all_skills}
    all_skill_paths: dict[str, Path] = {**registry.project_skills, **registry.fieldkit_skills}

    for skill_name, skill_path in all_skill_paths.items():
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError:
            continue

        refs = _extract_related_skill_names(text) + _extract_skills_use_names(text)
        for ref in refs:
            if ref in all_skills and ref != skill_name:
                adjacency[skill_name].add(ref)

    # ----------------------------------------------------------------
    # G001 — Cycle detection via Kahn's algorithm
    # ----------------------------------------------------------------
    in_degree: dict[str, int] = dict.fromkeys(all_skills, 0)
    for targets in adjacency.values():
        for tgt in targets:
            in_degree[tgt] = in_degree.get(tgt, 0) + 1

    queue: deque[str] = deque(name for name in all_skills if in_degree[name] == 0)
    processed = 0

    while queue:
        node = queue.popleft()
        processed += 1
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if processed < len(all_skills):
        cycle_nodes = sorted(name for name in all_skills if in_degree[name] > 0)
        cycle_msg = " ↔ ".join(cycle_nodes)
        rep_file = ""
        if cycle_nodes:
            first = cycle_nodes[0]
            if first in all_skill_paths:
                rep_file = str(all_skill_paths[first].relative_to(context.repo_root))
        violations.append(
            model.Violation(
                code="G001",
                file=rep_file,
                line=None,
                message=f"cycle detected in skill reference graph: {cycle_msg}",
            )
        )

    # ----------------------------------------------------------------
    # G002 — Orphan detection via BFS from agents + commands + user-invocable
    # ----------------------------------------------------------------
    reachable: set[str] = set()

    def _collect_refs_from_file(path: Path) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return _extract_related_skill_names(text) + _extract_skills_use_names(text)

    bfs_queue: deque[str] = deque()

    # Seed reachability from agents and commands (explicit cross-references).
    if context.agents_dir.is_dir():
        for f in context.agents_dir.iterdir():
            if f.is_file() and f.suffix == ".md":
                for ref in _collect_refs_from_file(f):
                    if ref in all_skills and ref not in reachable:
                        reachable.add(ref)
                        bfs_queue.append(ref)

    if context.commands_dir.is_dir():
        for f in context.commands_dir.iterdir():
            if f.is_file() and f.suffix == ".md":
                for ref in _collect_refs_from_file(f):
                    if ref in all_skills and ref not in reachable:
                        reachable.add(ref)
                        bfs_queue.append(ref)

    # Also seed from skills marked slash: true.
    # OpenCode v2 reads this field natively — it makes the skill available as a
    # slash command. A skill with slash: true has a declared invocation path and
    # does not need a cross-reference from an agent or command file to be reachable.
    #
    # NOTE: skills reachable only via global skills (~/.agents/skills/) or the
    # OpenCode runtime context will still appear as G002. This is expected per the
    # /check-skill-integrity spec — G002 is informational/non-blocking for those cases.
    for skill_name, skill_path in all_skill_paths.items():
        if skill_name in reachable:
            continue
        try:
            fm = corpus._parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if fm.get("slash") is True:
            reachable.add(skill_name)
            bfs_queue.append(skill_name)

    while bfs_queue:
        current = bfs_queue.popleft()
        for neighbor in adjacency.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                bfs_queue.append(neighbor)

    global_skill_note = (
        "global skills (~/.agents/skills/) are not included in reachability analysis;"
        " skills referenced only by global skills will appear here."
    )
    for skill_name in sorted(all_skills):
        if skill_name not in reachable:
            path = all_skill_paths[skill_name]
            violations.append(
                model.Violation(
                    code="G002",
                    file=str(path.relative_to(context.repo_root)),
                    line=None,
                    message=(
                        f"orphaned skill '{skill_name}' — not referenced by any agent,"
                        f" command, or skill in scope. Note: {global_skill_note}"
                    ),
                )
            )

    return violations
