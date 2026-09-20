"""Corpus parsing, registry construction, and schema validation."""

import json
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode

from skill_integrity import model


def _parse_frontmatter(text: str) -> dict[str, object]:
    """Parse YAML frontmatter from a markdown file (manual split on ---).

    Returns an empty dict if no frontmatter is found.
    Handles simple scalar values and boolean coercion only — no full YAML parser.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break

    if end == -1:
        return {}

    fm_lines = lines[1:end]
    result: dict[str, object] = {}
    for fm_line in fm_lines:
        if ":" not in fm_line:
            continue
        key, _, raw_val = fm_line.partition(":")
        key = key.strip()
        val: object = raw_val.strip()
        if not key or key.startswith("#"):
            continue
        # Coerce booleans
        if val == "true":
            val = True
        elif val == "false":
            val = False
        # Strip surrounding quotes
        elif isinstance(val, str) and len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            val = val[1:-1]
        result[key] = val
    return result


def _frontmatter_string(text: str, key: str) -> tuple[str, int] | None:
    """Return a parsed top-level string value and its one-based key line."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None

    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---")
        root = yaml.compose("".join(lines[1:end]))
    except (StopIteration, yaml.YAMLError):
        return None
    if not isinstance(root, MappingNode):
        return None
    match: tuple[str, int] | None = None
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode) or key_node.value != key:
            continue
        if not isinstance(value_node, ScalarNode) or value_node.tag != "tag:yaml.org,2002:str":
            match = None
            continue
        match = (value_node.value, key_node.start_mark.line + 2) if value_node.value else None
    return match


def _parse_jsonc(text: str) -> dict[str, object]:
    """Parse JSON with JavaScript comments and trailing commas."""
    without_comments: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            without_comments.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            without_comments.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index = text.find("\n", index + 2)
            if index == -1:
                break
            without_comments.append("\n")
            index += 1
            continue
        if char == "/" and following == "*":
            closing = text.find("*/", index + 2)
            index = len(text) if closing == -1 else closing + 2
            continue
        without_comments.append(char)
        index += 1

    normalized = "".join(without_comments)
    without_trailing_commas: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(normalized):
        if in_string:
            without_trailing_commas.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            following = normalized[index + 1 :].lstrip()
            if following.startswith(("}", "]")):
                continue
        without_trailing_commas.append(char)
    data = json.loads("".join(without_trailing_commas))
    return data if isinstance(data, dict) else {}


def _skill_dirs(base: Path) -> Iterator[tuple[str, Path]]:
    """Yield (name, skill_path) for each SKILL.md under base."""
    if not base.is_dir():
        return
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            yield skill_dir.name, skill_file


def _read_coverage_floor(context: model.IntegrityContext) -> int:
    """Read fail_under from [tool.coverage.report] in pyproject.toml."""
    try:
        with context.pyproject_toml.open("rb") as f:
            data = tomllib.load(f)
        return int(data.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under", 80))
    except (OSError, KeyError, ValueError, TypeError):
        return 80


def _read_crap_threshold(context: model.IntegrityContext) -> int:
    """Read --max-crapload N from Makefile, skipping comment lines."""
    try:
        text = context.makefile.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # Skip comment lines
            if stripped.startswith("#"):
                continue
            m = re.search(r"--max-crapload[= ](\d+)", stripped)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 1


def build_registry(context: model.IntegrityContext) -> model.Registry:
    """Walk the filesystem and build symbol tables for all corpus entities.

    Returns:
        Registry with all symbol tables populated.
    """
    reg = model.Registry()

    for name, path in _skill_dirs(context.project_skills_dir):
        reg.project_skills[name] = path

    for name, path in _skill_dirs(context.fieldkit_skills_dir):
        reg.fieldkit_skills[name] = path

    if context.agents_dir.is_dir():
        for f in context.agents_dir.iterdir():
            if f.is_file() and f.suffix == ".md":
                reg.agents.add(f.stem)

    if context.commands_dir.is_dir():
        for f in context.commands_dir.iterdir():
            if f.is_file() and f.suffix == ".md":
                reg.commands.add(f.stem)

    for config_path in (context.opencode_json, context.project_opencode_json):
        if not config_path.is_file():
            continue
        try:
            config_text = config_path.read_text(encoding="utf-8")
            data = _parse_jsonc(config_text) if config_path.suffix == ".jsonc" else json.loads(config_text)
            mcp = data.get("mcp", {})
            if isinstance(mcp, dict):
                reg.mcp_groups.update(mcp.keys())
            configured_agents = data.get("agent", {})
            if isinstance(configured_agents, dict):
                reg.agents.update(configured_agents.keys())
        except json.JSONDecodeError as e:
            print(
                f"WARNING: could not parse {config_path.name} ({e}); its registry entries will be skipped.",
                file=sys.stderr,
            )
        except OSError as e:
            print(
                f"WARNING: could not read {config_path.name} ({e}); its registry entries will be skipped.",
                file=sys.stderr,
            )

    reg.coverage_floor = _read_coverage_floor(context)
    reg.crap_threshold = _read_crap_threshold(context)

    return reg


# ---------------------------------------------------------------------------
# Layer 1 - Schema validation (S001-S004)
# ---------------------------------------------------------------------------


def validate_schemas(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Validate frontmatter schema for all skills in both namespaces.

    Args:
        registry: Populated corpus registry.

    Returns:
        List of schema violations.
    """
    violations: list[model.Violation] = []

    def _check_skill(name: str, path: Path, namespace: str) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return

        fm = _parse_frontmatter(text)
        rel = str(path.relative_to(context.repo_root))

        # S001 — missing required fields.
        # description is required in both namespaces.
        # name is required in fieldkit namespace only — UF-owned .opencode/ skills
        # intentionally omit name: because OpenCode derives it from the directory name.
        required = ["description"]
        if namespace == "fieldkit":
            required = ["name", "description"]
        for req_field in required:
            if req_field not in fm:
                violations.append(
                    model.Violation(
                        code="S001",
                        file=rel,
                        line=None,
                        message=f"missing required frontmatter field '{req_field}'",
                    )
                )

        # S002 — name field must match dirname
        if "name" in fm:
            fm_name = fm["name"]
            if isinstance(fm_name, str) and fm_name != name:
                violations.append(
                    model.Violation(
                        code="S002",
                        file=rel,
                        line=None,
                        message=f"name '{fm_name}' does not match directory name '{name}'",
                    )
                )

        # S003 — fieldkit skill has legacy user-invocable field (should be slash)
        # user-invocable is a local fiction we invented; OpenCode v2 reads slash: bool
        # instead. Flag any remaining occurrences so they can be migrated.
        if "user-invocable" in fm:
            violations.append(
                model.Violation(
                    code="S003",
                    file=rel,
                    line=None,
                    message="legacy 'user-invocable' field found — replace with 'slash: true' if slash-invocable, or remove",
                )
            )

        # S004 — wrong type for slash
        if "slash" in fm and not isinstance(fm["slash"], bool):
            violations.append(
                model.Violation(
                    code="S004",
                    file=rel,
                    line=None,
                    message=(f"field 'slash' has type {type(fm['slash']).__name__!r}, expected boolean"),
                )
            )

    for name, path in registry.project_skills.items():
        _check_skill(name, path, "project")

    for name, path in registry.fieldkit_skills.items():
        _check_skill(name, path, "fieldkit")

    return violations
