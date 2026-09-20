"""Selection and path-policy helpers for skill installation targets."""

from collections.abc import Callable
from pathlib import Path

from fieldkit.commands.skill._output import human_echo
from fieldkit.skill.install import detect_tools
from fieldkit.skill.targets import GLOBAL_SKILL_NAMES, TOOL_TARGETS, InstallTarget

ToolSelector = Callable[[list[str], list[str]], list[str]]


def validate_global_install_request(
    tools: list[str], skills: list[str], *, install_all: bool, prune: bool
) -> str | None:
    """Validate the bounded global install option matrix."""
    unsupported_tool = next((tool for tool in tools if tool not in {"opencode", "claude-code"}), None)
    unsupported_skill = next((skill for skill in skills if skill not in GLOBAL_SKILL_NAMES), None)
    errors = (
        (not tools, "--global requires at least one explicit --tool."),
        (
            unsupported_tool is not None,
            f"--global does not support tool {unsupported_tool!r}; use opencode or claude-code.",
        ),
        (install_all, "--global does not support --all; select handoffs and/or pickup explicitly."),
        (not prune and not skills, "--global requires --skill handoffs and/or --skill pickup."),
        (
            unsupported_skill is not None,
            f"--global does not support skill {unsupported_skill!r}; use handoffs or pickup.",
        ),
    )
    return next((message for invalid, message in errors if invalid), None)


def prepare_install_targets(
    tools: list[str],
    cwd: Path,
    *,
    global_install: bool,
    select_tools: ToolSelector,
) -> tuple[list[InstallTarget], int | None]:
    """Select tools and resolve their targets, returning an early exit when needed."""
    if global_install:
        selected_tools = tools
    else:
        detected_tools = detect_tools(cwd)
        if cwd.resolve() == Path.home().resolve():
            home_tools = {"opencode", "claude-code"}
            if home_tools.intersection(tools):
                human_echo(
                    "OpenCode and Claude Code project-local installs are not allowed from the user home.", err=True
                )
                return [], 1
            detected_tools = [tool for tool in detected_tools if tool not in home_tools]
        selected_tools = select_tools(detected_tools, tools)
    if not selected_tools:
        human_echo("No tools selected. Nothing installed.")
        return [], 0
    targets, error = resolve_install_targets(selected_tools, cwd, global_install=global_install)
    if error:
        human_echo(error, err=True)
        return [], 1
    return targets, None


def resolve_install_targets(
    tool_keys: list[str], cwd: Path, *, global_install: bool
) -> tuple[list[InstallTarget], str | None]:
    """Resolve registered destinations and map path failures to validation errors."""
    try:
        return _resolve_install_targets(tool_keys, cwd, global_install=global_install), None
    except (OSError, RuntimeError, ValueError) as exc:
        return [], f"Could not validate registered skill root: {exc}"


def _resolve_install_targets(tool_keys: list[str], cwd: Path, *, global_install: bool) -> list[InstallTarget]:
    """Build canonical descriptors or raise when a registered root is unsafe."""
    base = cwd.resolve()
    home = Path.home().resolve()
    agents_parent = home / ".agents"
    claude_parent = home / ".claude"
    opencode_root = (agents_parent / "skills").resolve()
    targets: list[InstallTarget] = []
    seen_roots: set[Path] = set()

    def validate_parent(parent: Path) -> None:
        if parent.is_symlink() or parent.resolve().parent != home:
            raise ValueError(f"registered global parent is not a trusted home directory: {parent.name}")

    for key in tool_keys:
        registered = TOOL_TARGETS[key]
        if global_install:
            if registered.global_skill_root is None:
                raise ValueError(f"tool {key!r} has no registered global skill root")
            skill_root = (home / registered.global_skill_root).resolve()
            if key == "opencode":
                validate_parent(agents_parent)
                if not skill_root.is_relative_to(agents_parent.resolve()):
                    raise ValueError("OpenCode global skill root escapes its registered parent")
            else:
                validate_parent(claude_parent)
                if not skill_root.is_relative_to(claude_parent.resolve()):
                    validate_parent(agents_parent)
                    if skill_root != opencode_root:
                        raise ValueError("Claude Code global skill root escapes its registered parent")
        else:
            skill_root = (base / registered.skill_root).resolve()
            if not skill_root.is_relative_to(base):
                raise ValueError(f"registered skill root escapes project directory: {registered.label}")
        if skill_root in seen_roots:
            continue
        seen_roots.add(skill_root)
        targets.append(
            InstallTarget(
                key=key,
                label=registered.label,
                skill_root=skill_root,
                manifest_path=skill_root / registered.manifest_name,
                skill_path=registered.skill_path,
                format=registered.format,
            )
        )
    return targets
