"""Dead-reference validation for the skill-integrity corpus."""

import re
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

from skill_integrity import corpus, model

# Workspace-data path prefixes excluded from R005 path checks.
# These are paths in the AE OS workspace (fieldkit_home), not the code repo.
WORKSPACE_DATA_PREFIXES = (
    "accounts/",
    "memory/",
    "watchers/",
    "briefs/",
    "config/",  # config/ is a workspace dir (config/accounts.yaml, config/identity.yaml)
    "data/",  # data/ is the runtime artifacts dir under fieldkit_home
    "playbooks/",  # playbooks/ is a workspace dir (playbooks/meddpicc.md, etc.)
    "skills/",  # skills/ is a workspace dir (skills/meeting/brief-template.md)
    "reports/",  # reports/ is a generated output dir (reports/skill-integrity.json)
    "scratch/",  # scratch/ is the gitignored operator scratch dir (scratch/out/brief.json)
)

# MCP group names that are valid mcpjungle group identifiers referenced by fieldkit
# skills but served through a different OpenCode server key (for example,
# backstory is accessed via fieldkit-sales). These are known-good
# mcpjungle group names that should not trigger R002.
KNOWN_MCPJUNGLE_GROUPS = frozenset(
    {
        "backstory",  # served via fieldkit-sales MCP group
        "github",  # GitHub MCP group (not in fieldkit- prefix)
    }
)
# To add a new entry: verify the group is live in mcpjungle AND confirm it is
# intentionally served under a different opencode.json server key. Document the
# mcpjungle → opencode.json key mapping in the comment below.
# backstory → served via fieldkit-sales
# github → external GitHub MCP server (not in fieldkit- prefix)
# serena → registered as general-serena in opencode.json

# Global skills that live in ~/.agents/skills/ and are installable but are not
# part of this repo's skill corpus. R001 checks skip these names so that
# Related Skills entries pointing to global skills don't false-positive.
# To add a new entry: confirm the skill exists in the global skill marketplace
# and is expected to be installed on the target machine.
#
# NOTE: keep this set as small as possible. Before adding a skill here, ask:
# "Should this skill live in src/fieldkit/skills/ instead?" If it has no
# fieldkit-specific logic, it probably belongs in the repo so CI can verify it.
KNOWN_GLOBAL_SKILLS: frozenset[str] = frozenset()

# R003: agent names valid in orchestration harness with no corresponding .opencode/agents/ file.
# Keep minimal — add only agents confirmed to be intentionally harness-only.
KNOWN_HARNESS_AGENT_TYPES: frozenset[str] = frozenset()

# R004: command references must be a complete identifier (no trailing hyphen)
# Pattern: `/command-name` where name is at least 2 chars and ends with alnum
_R004_PATTERN = re.compile(r"`(/[a-z][a-z0-9-]*[a-z0-9])`")

# R002: MCP group references via double-underscore convention.
# Claude Code exposes MCP tools as `mcp__<server>__<tool>`, where the leading `mcp__`
# is a client prefix rather than a group name. Match that form separately so the
# server name is what gets validated, and exclude it from the generic pattern (which
# would otherwise capture the literal "mcp" as the group).
_R002_CLIENT_PREFIX_PATTERN = re.compile(r"\bmcp__([a-z][a-z0-9-]+)__")
_R002_PATTERN = re.compile(r"\b(?!mcp__)([a-z][a-z0-9-]+)__[a-z_]+\b")

# R005: backtick-quoted relative file paths ending in .md/.json/.yaml
# Must contain at least one directory separator (/) to distinguish path references
# from concept references like `tasks.md` used as generic filenames in prose.
# Single-component filenames (no /) are concept references, not path references.
_R005_PATTERN = re.compile(r"`([a-z][a-z0-9_-]*/[a-z0-9/_-]+\.(?:md|json|yaml))`")

# R006: skills_use({name: "..."}) references
_R006_PATTERN = re.compile(r'skills_use\s*\(\s*\{[^}]*name\s*:\s*["\']([^"\']+)["\']')

# R001: Related Skills entry formats
_R001_BOLD = re.compile(r"^\s*-\s+\*\*([a-z][a-z0-9-]+)\*\*")
_R001_BACKTICK = re.compile(r"^\s*-\s+`/([a-z][a-z0-9-]+)`")

# R003: agent name references — look for backtick-quoted identifiers that match
# agent naming convention (hyphenated lowercase) and check against registry.
# We look for patterns like `agent-name` in backtick context.
_R003_BACKTICK_ID = re.compile(r"`([a-z][a-z0-9-]+(?:-[a-z0-9]+)+)`")


def _split_body_and_related_skills(text: str) -> tuple[str, str, int]:
    """Split file text into (body_without_related_skills, related_skills_section, rs_start_line).

    rs_start_line is the 1-based line number where the Related Skills section begins.
    Returns rs_start_line=0 if no Related Skills section found.
    """
    rs_pattern = re.compile(r"^## Related Skills\s*$", re.MULTILINE)
    m = rs_pattern.search(text)
    if not m:
        return text, "", 0

    rs_start = m.start()
    rs_start_line = text[:rs_start].count("\n") + 1

    # Find the next ## heading after the Related Skills section
    next_heading = re.search(r"^## ", text[m.end() :], re.MULTILINE)
    rs_end = m.end() + next_heading.start() if next_heading else len(text)

    related_section = text[rs_start:rs_end]
    body_without_rs = text[:rs_start] + text[rs_end:]
    return body_without_rs, related_section, rs_start_line


def _strip_fenced_code_blocks(text: str) -> str:
    """Replace content inside fenced code blocks with blank lines (preserves line numbers)."""
    result: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not in_fence:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                in_fence = True
                fence_marker = m.group(1)[0] * len(m.group(1))
                result.append("\n")
            else:
                result.append(line)
        else:
            if stripped.startswith(fence_marker) and not stripped[len(fence_marker) :].strip():
                in_fence = False
                result.append("\n")
            else:
                result.append("\n")
    return "".join(result)


def _all_md_files(context: model.IntegrityContext, registry: model.Registry) -> Iterator[Path]:
    """Yield all .md files to scan: skills + agents + commands."""
    for path in registry.project_skills.values():
        yield path
    for path in registry.fieldkit_skills.values():
        yield path
    if context.agents_dir.is_dir():
        for f in sorted(context.agents_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                yield f
    if context.commands_dir.is_dir():
        for f in sorted(context.commands_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                yield f


def _all_skill_names(registry: model.Registry) -> set[str]:
    return set(registry.project_skills) | set(registry.fieldkit_skills)


def check_dead_refs(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Detect dead references across all corpus files (R001-R006).

    Args:
        registry: Populated corpus registry.

    Returns:
        List of dead-reference violations.
    """
    violations: list[model.Violation] = []
    all_skills = _all_skill_names(registry)

    # Pre-compute set of agent names for R003 lookup
    agent_names = registry.agents

    for md_path in _all_md_files(context, registry):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue

        rel_path = str(md_path.relative_to(context.repo_root))
        lines_list = text.splitlines()

        _, related_section, rs_start_line = _split_body_and_related_skills(text)

        # Compute the set of line numbers that belong to the Related Skills section
        rs_line_nums: set[int] = set()
        if rs_start_line > 0 and related_section:
            rs_end_line = rs_start_line + related_section.count("\n")
            rs_line_nums = set(range(rs_start_line, rs_end_line + 1))

        # ----------------------------------------------------------------
        # R001 — dead Related Skills entries
        # ----------------------------------------------------------------
        if related_section:
            for i, rs_line in enumerate(related_section.splitlines()):
                lineno = rs_start_line + i

                # Bold format: - **skill-name** — ...
                m_bold = _R001_BOLD.match(rs_line)
                if m_bold:
                    skill_name = m_bold.group(1)
                    if skill_name not in all_skills and skill_name not in KNOWN_GLOBAL_SKILLS:
                        violations.append(
                            model.Violation(
                                code="R001",
                                file=rel_path,
                                line=lineno,
                                message=f"Related Skills entry '{skill_name}' not found in any skill namespace",
                            )
                        )
                    continue

                # Backtick-slash format: - `/skill-name` — ...
                m_bt = _R001_BACKTICK.match(rs_line)
                if m_bt:
                    skill_name = m_bt.group(1)
                    if skill_name not in all_skills and skill_name not in KNOWN_GLOBAL_SKILLS:
                        violations.append(
                            model.Violation(
                                code="R001",
                                file=rel_path,
                                line=lineno,
                                message=f"Related Skills entry '/{skill_name}' not found in any skill namespace",
                            )
                        )

        # ----------------------------------------------------------------
        # R002 — unregistered MCP groups (search full file body)
        # ----------------------------------------------------------------
        for i, line in enumerate(lines_list, start=1):
            for m in chain(
                _R002_CLIENT_PREFIX_PATTERN.finditer(line),
                _R002_PATTERN.finditer(line),
            ):
                group = m.group(1)
                if group not in registry.mcp_groups and group not in KNOWN_MCPJUNGLE_GROUPS:
                    violations.append(
                        model.Violation(
                            code="R002",
                            file=rel_path,
                            line=i,
                            message=f"MCP group '{group}' not in opencode.json",
                        )
                    )

        # ----------------------------------------------------------------
        # R003 — hardcoded agent name references
        # (a) frontmatter agent: key — validate against known agent union
        # (b) backtick identifiers in explicit .opencode/agents/ path context
        # ----------------------------------------------------------------
        _r003_known = agent_names | KNOWN_HARNESS_AGENT_TYPES
        fm_agent = corpus._frontmatter_string(text, "agent")
        if fm_agent is not None and fm_agent[0] not in _r003_known:
            fm_agent_val, fm_agent_line = fm_agent
            violations.append(
                model.Violation(
                    code="R003",
                    file=rel_path,
                    line=fm_agent_line,
                    message=f"agent '{fm_agent_val}' in frontmatter not found in .opencode/agents/",
                )
            )
        for i, line in enumerate(lines_list, start=1):
            # Skip lines that look like glob patterns (dynamic discovery)
            if re.search(r"\*\.\w+", line):
                continue
            # Only check lines that explicitly reference .opencode/agents/ paths
            if ".opencode/agents/" not in line:
                continue
            for m in _R003_BACKTICK_ID.finditer(line):
                candidate = m.group(1)
                # Skip known skills, commands, and MCP groups
                if candidate in all_skills or candidate in registry.commands or candidate in registry.mcp_groups:
                    continue
                # Flag if the candidate is not a known agent
                if candidate not in _r003_known:
                    violations.append(
                        model.Violation(
                            code="R003",
                            file=rel_path,
                            line=i,
                            message=f"agent '{candidate}' not found in .opencode/agents/",
                        )
                    )

        # ----------------------------------------------------------------
        # R004 — command references in body (excluding Related Skills section)
        # A /name reference is valid if it matches a command file OR a skill name
        # (since skills can be invoked as slash commands by their name).
        # ----------------------------------------------------------------
        for i, line in enumerate(lines_list, start=1):
            if i in rs_line_nums:
                continue
            for m in _R004_PATTERN.finditer(line):
                cmd_ref = m.group(1)  # e.g., "/opsx-continue"
                cmd_stem = cmd_ref.lstrip("/")
                if cmd_stem not in registry.commands and cmd_stem not in all_skills:
                    violations.append(
                        model.Violation(
                            code="R004",
                            file=rel_path,
                            line=i,
                            message=f"command '{cmd_ref}' not found in .opencode/commands/ or any skill namespace",
                        )
                    )

        # ----------------------------------------------------------------
        # R005 — path references that don't exist (excluding fenced code blocks
        #         and data-repo paths)
        # ----------------------------------------------------------------
        text_no_fences = _strip_fenced_code_blocks(text)
        for i, line in enumerate(text_no_fences.splitlines(), start=1):
            for m in _R005_PATTERN.finditer(line):
                ref_path = m.group(1)
                # Exclude data-repo paths
                if any(ref_path.startswith(prefix) for prefix in WORKSPACE_DATA_PREFIXES):
                    continue
                # Resolve against repo root, then against the file's parent dir
                resolved = (context.repo_root / ref_path).resolve()
                resolved_local = (md_path.parent / ref_path).resolve()
                try:
                    resolved.relative_to(context.repo_root.resolve())
                except ValueError:
                    continue  # path escaped repo root — skip
                try:
                    resolved_local.relative_to(context.repo_root.resolve())
                except ValueError:
                    resolved_local = None  # type: ignore[assignment]
                if not resolved.exists() and (resolved_local is None or not resolved_local.exists()):
                    violations.append(
                        model.Violation(
                            code="R005",
                            file=rel_path,
                            line=i,
                            message=f"path reference '{ref_path}' does not exist on disk",
                        )
                    )

        # ----------------------------------------------------------------
        # R006 — skills_use() references to non-existent skills
        # ----------------------------------------------------------------
        for i, line in enumerate(lines_list, start=1):
            for m in _R006_PATTERN.finditer(line):
                skill_ref = m.group(1)
                if skill_ref not in all_skills:
                    violations.append(
                        model.Violation(
                            code="R006",
                            file=rel_path,
                            line=i,
                            message=f"skills_use name '{skill_ref}' not found in any skill namespace",
                        )
                    )

    return violations
