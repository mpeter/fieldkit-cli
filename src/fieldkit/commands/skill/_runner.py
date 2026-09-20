"""fieldkit skill list / show / install — skill discovery, inspection, and installation."""

import functools
import json
import logging
import sys
from pathlib import Path
from typing import Any

from fieldkit.commands.skill._install_execution import _apply_install_plan, _echo_install_summary
from fieldkit.commands.skill._output import InstallItemOutcome, json_enabled
from fieldkit.commands.skill._output import human_echo as _echo
from fieldkit.commands.skill._target_resolution import prepare_install_targets, validate_global_install_request
from fieldkit.config import CONFIG_PATH
from fieldkit.pursuit.io import extract_frontmatter_text

# ---------------------------------------------------------------------------
# Optional questionary dependency (interactive checkbox prompts)
# ---------------------------------------------------------------------------

questionary: Any = None
try:
    import questionary

    HAS_QUESTIONARY = True
except ImportError:
    HAS_QUESTIONARY = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill directory resolution
# ---------------------------------------------------------------------------


@functools.cache
def _skills_dir() -> Path:
    """Return the skills/ directory, resolved in priority order.

    Resolution order:
    1. FIELDKIT_SKILLS_DIR env var (explicit override)
    2. fieldkit_root key in the active fieldkit config → <root>/skills/
       (works when fieldkit is installed via `uv tool install` and skills/ lives
       in the development repo alongside the source, not in site-packages)
    3. Package-relative path: fieldkit-tools/skills/
       (works during development when running `uv run fieldkit`)
    """
    import os

    # 1. Explicit env var override
    env_override = os.environ.get("FIELDKIT_SKILLS_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    # 2. Config-based resolution via fieldkit_root
    #    A configured checkout may expose skills through .agents/skills or skills/.
    config_path = CONFIG_PATH
    if config_path.exists():
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "fieldkit_root" in data:
                root = Path(str(data["fieldkit_root"])).expanduser().resolve()
                # Prefer the agent-visible skills directory when it is available.
                for subpath in (".agents/skills", "skills"):
                    candidate = root / subpath
                    if candidate.is_dir():
                        return candidate
        except Exception:  # noqa: BLE001
            logger.debug("skill._runner: failed to resolve skills dir from config fieldkit_root", exc_info=True)
            pass  # Fall through to package-relative path

    # 3. importlib.resources fallback for installed builds (skills/ packaged inside fieldkit/)
    try:
        import importlib.resources

        ref = importlib.resources.files("fieldkit.skills")
        candidate = Path(str(ref))
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, TypeError):
        logger.debug(
            "skill._runner: importlib.resources lookup failed; falling back to package-relative path", exc_info=True
        )

    # 4. Package-relative fallback (dev: fieldkit/skill/_runner.py → fieldkit/skills/)
    #    Note: fieldkit/skills/ is now the sole canonical source for bundled skills.
    #    The old bare skills/ directory at the repo root no longer exists; this
    #    candidate will simply not be found, causing resolution to fall through to
    #    step 3 (importlib.resources) or the env-var / config-based paths above.
    import importlib.resources

    try:
        return Path(str(importlib.resources.files("fieldkit.skills")))
    except Exception:  # noqa: BLE001
        # Last resort: skill/ -> commands/ -> fieldkit/ -> src/ -> repo root -> skills/
        here = Path(__file__).resolve()
        return here.parent.parent.parent.parent.parent / "skills"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-ish frontmatter from a SKILL.md file.

    Handles:
    - Simple `key: value` fields
    - Block scalars: `key: >` followed by indented continuation lines
    """
    fm_text = extract_frontmatter_text(text)
    if not fm_text:
        return {}

    result: dict[str, str] = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line or line.startswith(" "):
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"')

        # Skip malformed / non-simple keys
        if not key or " " in key or len(key) > 40:
            i += 1
            continue

        if value in (">", ">-", "|", "|-"):
            # YAML block scalar (folded > or literal |) — collect indented continuation lines
            parts = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or lines[i].strip() == ""):
                parts.append(lines[i].strip())
                i += 1
            result[key] = " ".join(p for p in parts if p)
        else:
            result[key] = value
            i += 1

    return result


def _load_skill(skill_dir: Path) -> dict[str, Any] | None:
    """Load a skill directory into a normalized dict. Returns None if invalid."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    text = skill_md.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    name = fm.get("name") or skill_dir.name

    # Resolve description — may be multi-sentence; truncate cleanly for list view
    description = fm.get("description", "")

    # Normalize groups-needed field (two spellings in the wild)
    groups_needed = fm.get("groups-needed") or fm.get("groups_needed") or ""

    return {
        "name": name,
        "dir": skill_dir.name,
        "description": description,
        "user_invocable": fm.get("user-invocable", "true").lower() == "true",
        "argument_hint": fm.get("argument-hint", ""),
        "groups_needed": groups_needed,
        "version": fm.get("version", ""),
        "path": str(skill_md),
        "has_evals": (skill_dir / "evals" / "evals.json").exists(),
    }


@functools.cache
def _load_all_skills() -> list[dict[str, Any]]:
    skills_dir = _skills_dir()
    if not skills_dir.is_dir():
        return []
    results = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill = _load_skill(d)
        if skill:
            results.append(skill)
    return results


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_list_human(skills: list[dict[str, Any]], *, verbose: bool = False) -> None:
    if not skills:
        _echo("No skills found.")
        return
    _echo(f"{'NAME':<28}  {'E':>1}  {'DESCRIPTION'}")
    _echo(f"{'-' * 28}  {'-':>1}  {'-' * 60}")
    for s in skills:
        description = s["description"]
        full_desc = description.strip() if description else ""
        desc = full_desc if verbose else full_desc.split(".")[0].strip()
        evals_marker = "✓" if s["has_evals"] else " "
        rendered_desc = desc if verbose else _truncate(desc, 70)
        _echo(f"{s['name']:<28}  {evals_marker:>1}  {rendered_desc}")
    _echo()
    with_evals = sum(1 for s in skills if s["has_evals"])
    _echo(
        f"{len(skills)} skills available ({with_evals} with evals)."
        "  E=has evals.  Use `fieldkit skill show <name>` for details."
    )


def _render_show_human(skill: dict[str, Any]) -> None:
    _echo(f"Skill: {skill['name']}")
    # implementation change: suppress version line when empty
    if skill["version"]:
        _echo(f"Version:         {skill['version']}")
    # implementation change: display argument_hint when non-empty
    if skill["argument_hint"]:
        _echo(f"Argument hint:   {skill['argument_hint']}")
    _echo()
    _echo("Description:")
    # Word-wrap description at 80 chars
    words = skill["description"].split()
    line = "  "
    for word in words:
        if len(line) + len(word) + 1 > 80:
            _echo(line)
            line = "  " + word
        else:
            line = line + (" " if line != "  " else "") + word
    if line.strip():
        _echo(line)
    _echo()
    # historic regression: The argument-hint field contained fake CLI flags that implied
    # a non-existent CLI command. The Usage line is removed entirely to avoid
    # misleading users. Skill invocation is always via the agent, not the CLI.
    _echo(f"User-invocable:  {'yes' if skill['user_invocable'] else 'no'}")
    # implementation change: suppress groups_needed line when empty
    if skill["groups_needed"]:
        _echo(f"MCP groups:      {skill['groups_needed']}")
    _echo(f"Has evals:       {'yes' if skill['has_evals'] else 'no'}")
    _echo(f"Path:            {skill['path']}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _validate_skill_name(name: str) -> str | None:
    """Return an error message if *name* is not a safe, known skill name, else None.

    Checks for path-traversal characters first, then validates against the
    known skill corpus.  Returns None when the name is valid.
    """
    # Guard: reject path-traversal characters before any corpus lookup
    if ".." in name or "/" in name or "\\" in name:
        return f"Invalid skill name: {name!r}. Skill names must not contain path separators."

    skills_dir = _skills_dir()
    if not (skills_dir / name).is_dir():
        return f"Unknown skill: {name!r}. Run `fieldkit skill list` to see available skills."
    return None


def _numbered_select(prompt: str, choices: list[str]) -> list[str]:
    """Present a numbered list and return the user-selected items.

    Accepts a comma-separated list of 1-based indices (e.g. ``"1,3"``).
    An empty response returns an empty list.  Invalid indices are ignored.
    """
    _echo(prompt)
    for i, choice in enumerate(choices, 1):
        _echo(f"  {i}. {choice}")
    raw = input("Enter numbers (comma-separated, or Enter to skip): ").strip()
    if not raw:
        return []
    selected: list[str] = []
    for raw_part in raw.split(","):
        stripped = raw_part.strip()
        if stripped.isdigit():
            idx = int(stripped) - 1
            if 0 <= idx < len(choices):
                selected.append(choices[idx])
    return selected


# ---------------------------------------------------------------------------
# _cmd_install helpers (extracted to reduce cyclomatic complexity)
# ---------------------------------------------------------------------------


def _select_tools(
    detected_tools: list[str],
    pre_selected: list[str],
    *,
    has_questionary: bool,
) -> list[str]:
    """Interactive or pre-selected tool selection. Returns selected tool keys.

    Args:
        detected_tools: Tool keys detected in CWD (pre-checked in interactive mode).
        pre_selected:   Tool keys from ``--tool`` flags. When non-empty, returned
                        directly without prompting.
        has_questionary: Whether the questionary library is available and stdin is a TTY.

    Returns:
        List of selected tool keys (may be empty if the user skips selection).
    """
    from fieldkit.skill.targets import TOOL_TARGETS

    if pre_selected:
        return list(pre_selected)

    if not detected_tools:
        _echo("Note: no tool config was detected in the current directory.")
        _echo("      All tools are shown unchecked. Select the tools you want to install into.")

    tool_keys = list(TOOL_TARGETS.keys())
    tool_labels = [TOOL_TARGETS[k].label for k in tool_keys]

    if has_questionary:
        tool_choices = [
            questionary.Choice(title=TOOL_TARGETS[k].label, checked=(k in detected_tools)) for k in tool_keys
        ]
        result_labels = questionary.checkbox(
            "Select tools to install skills into:",
            choices=tool_choices,
        ).ask()
        if result_labels is None:
            result_labels = []
        label_to_key = {TOOL_TARGETS[k].label: k for k in tool_keys}
        return [label_to_key[lbl] for lbl in result_labels if lbl in label_to_key]

    chosen_labels = _numbered_select("Select tools (by number):", tool_labels)
    label_to_key = {TOOL_TARGETS[k].label: k for k in tool_keys}
    return [label_to_key[lbl] for lbl in chosen_labels if lbl in label_to_key]


def _select_skills(
    pre_selected: list[str],
    *,
    has_questionary: bool,
) -> list[str]:
    """Interactive or pre-selected skill selection. Returns selected skill names.

    Args:
        pre_selected:    Skill names from ``--skill`` flags. When non-empty,
                         returned directly without prompting.
        has_questionary: Whether the questionary library is available and stdin is a TTY.

    Returns:
        List of selected skill names (may be empty if the user skips selection).
    """
    from fieldkit.skill.targets import SKILL_CATEGORIES

    if pre_selected:
        return list(pre_selected)

    # Build flat ordered list of skill names from categories
    all_categorised: list[str] = []
    for _cat, skill_names in SKILL_CATEGORIES.items():
        all_categorised.extend(skill_names)

    if has_questionary:
        choices: list[Any] = []
        for cat, skill_names in SKILL_CATEGORIES.items():
            choices.append(questionary.Separator(f"── {cat} ──"))
            for sn in skill_names:
                choices.append(sn)
        result_skills = questionary.checkbox(
            "Select skills to install:",
            choices=choices,
        ).ask()
        return result_skills if result_skills else []

    return _numbered_select("Select skills (by number):", all_categorised)


def _suggest_related(
    selected_skills: list[str],
    skills_dir: Path,
    all_skill_names: set[str],
    *,
    has_questionary: bool,
) -> list[str]:
    """Show related-skill suggestions and return any additions the user selected.

    Args:
        selected_skills: Skills already chosen by the user.
        skills_dir:      Path to the skills directory (for graph building).
        all_skill_names: Set of all known skill names (used to filter suggestions).
        has_questionary: Whether the questionary library is available and stdin is a TTY.

    Returns:
        List of additional skill names to add (may be empty).
    """
    from fieldkit.skill.graph import build_related_graph

    related_graph = build_related_graph(skills_dir)
    suggestions: set[str] = set()
    for sn in selected_skills:
        for related in related_graph.get(sn, set()):
            if related not in selected_skills:
                suggestions.add(related)

    if not suggestions:
        return []

    suggestion_list = sorted(suggestions)

    if has_questionary:
        extra = questionary.checkbox(
            "These related skills are commonly used alongside your selection. Add any?",
            choices=suggestion_list,
        ).ask()
        return [s for s in (extra or []) if s not in selected_skills]

    extra = _numbered_select("Suggested related skills (optional):", suggestion_list)
    return [s for s in extra if s not in selected_skills]


def _validate_flags(tools: list[str], skills: list[str]) -> str | None:
    """Validate --tool and --skill flag values before any I/O.

    Returns an error message string if validation fails, or None on success.
    """
    from fieldkit.skill.targets import TOOL_TARGETS

    for tool_key in tools:
        if tool_key not in TOOL_TARGETS:
            supported = ", ".join(sorted(TOOL_TARGETS))
            return f"Unknown tool: {tool_key!r}. Supported tools: {supported}."

    for skill_name in skills:
        err = _validate_skill_name(skill_name)
        if err:
            return err

    return None


def _cmd_install(
    tools: list[str],
    skills: list[str],
    *,
    install_all: bool = False,
    global_install: bool = False,
    dry_run: bool = False,
    prune: bool = False,
    confirm: bool = False,
    force: bool = False,
) -> int:
    """Install selected skills into project-local paths for the selected tools.

    This is the redesigned interactive install command (task 4.1).  It replaces
    the old global-install behaviour with a project-local, multi-tool-aware flow:

    1. Validate ``--tool`` and ``--skill`` flag values (if provided).
    2. Detect which AI tools are present in CWD (pre-check detected tools).
    3. Prompt for tool selection (questionary checkbox or numbered fallback).
    4. Prompt for skill selection grouped by category.
    5. Suggest related skills (one hop from the related-skill graph).
    6. Install selected skills to the correct project-local path per tool.
    7. Optionally preview or remove stale installer-owned skills.
    8. Print a per-skill summary and final count line.

    Args:
        tools:       Pre-selected tool keys (from ``--tool`` flags).  When non-empty,
                     the tool selection prompt is skipped.
        skills:      Pre-selected skill names (from ``--skill`` flags).  When
                     non-empty, the skill selection prompt is skipped.
        install_all: When True, install every skill in SKILL_CATEGORIES without
                     interactive selection.  Equivalent to passing every skill name
                     via ``--skill``.  Useful for ``make install`` automation.
        dry_run:     If True, print planned actions without writing any files.
        prune:       If True, inventory and preview stale installer-owned skills.
        confirm:     If True with ``prune``, remove the previewed stale skills.
        force:       If True, overwrite locally modified or untrusted skill targets.

    Returns:
        0 on success (all writes succeeded), 1 on any validation or write error.
    """
    from fieldkit.skill.install import discover_skill_names, expand_all_skills
    from fieldkit.skill.template import build_template_ctx

    cwd = Path.cwd()

    # ------------------------------------------------------------------
    # Step 1 — Validate flag values before any I/O
    # ------------------------------------------------------------------
    err = _validate_install_request(
        list(tools),
        list(skills),
        install_all=install_all,
        global_install=global_install,
        dry_run=dry_run,
        prune=prune,
        confirm=confirm,
    )
    if err:
        _echo(err, err=True)
        return 1

    # --all expands to every skill in SKILL_CATEGORIES after global validation.
    if install_all:
        skills = expand_all_skills()

    # ------------------------------------------------------------------
    # Steps 2-3 — Tool detection (pre-check state), then selection
    # ------------------------------------------------------------------
    use_questionary = HAS_QUESTIONARY and sys.stdin.isatty()
    targets, preparation_exit = prepare_install_targets(
        list(tools),
        cwd,
        global_install=global_install,
        select_tools=lambda detected, selected: _select_tools(detected, selected, has_questionary=use_questionary),
    )
    if preparation_exit is not None:
        return preparation_exit

    # ------------------------------------------------------------------
    # Step 4 — Skill selection (skip when --skill flags provided or --all)
    # ------------------------------------------------------------------
    selected_skills = _select_install_skills(list(skills), prune=prune, has_questionary=use_questionary)

    if not selected_skills and not prune:
        _echo("No skills selected. Nothing installed.")
        return 0

    skills_dir = _skills_dir()

    # ------------------------------------------------------------------
    # Step 5 — Related-skill suggestions (only in interactive mode)
    # ------------------------------------------------------------------
    if not tools and not skills:
        # Only suggest when the user went through the interactive flow
        selected_skills = selected_skills + _suggest_related(
            selected_skills,
            skills_dir,
            discover_skill_names(skills_dir),
            has_questionary=use_questionary,
        )

    # ------------------------------------------------------------------
    # Step 6 — Install
    # ------------------------------------------------------------------
    outcomes: list[InstallItemOutcome] | None = [] if json_enabled() else None
    total_new, total_updated, total_unchanged, total_errors = _apply_install_plan(
        targets,
        selected_skills,
        skills_dir,
        dry_run=dry_run,
        prune=prune,
        confirm=confirm,
        force=force,
        build_template_ctx=build_template_ctx,
        outcomes=outcomes,
    )

    # ------------------------------------------------------------------
    # Step 8 — Summary
    # ------------------------------------------------------------------
    _echo_install_summary(
        tool_count=len(targets),
        total_new=total_new,
        total_updated=total_updated,
        total_unchanged=total_unchanged,
        total_errors=total_errors,
        dry_run=dry_run,
        outcomes=outcomes,
    )
    return 0 if total_errors == 0 else 1


def _validate_install_request(
    tools: list[str],
    skills: list[str],
    *,
    install_all: bool,
    global_install: bool,
    dry_run: bool,
    prune: bool,
    confirm: bool,
) -> str | None:
    """Validate install and prune options before any filesystem work."""
    if confirm and not prune:
        return "--confirm requires --prune."
    if global_install:
        return validate_global_install_request(tools, skills, install_all=install_all, prune=prune)
    if prune:
        if dry_run and not tools:
            return "--dry-run with --prune requires --tool to avoid interactive prompts."
        return _validate_flags(tools, skills)
    return _validate_flags(tools, skills) or _validate_dry_run(tools, skills, dry_run=dry_run)


def _validate_dry_run(tools: list[str], skills: list[str], *, dry_run: bool) -> str | None:
    """Return an error when --dry-run would fall through into interactive prompts.

    implementation change: --dry-run is used by automation, so it must never block on a
    questionary prompt waiting for a selection nobody is there to make.
    """
    if dry_run and (not tools or not skills):
        return (
            "--dry-run requires --tool and --skill (or --all) to avoid interactive "
            "prompts. Example: fieldkit skill install --dry-run --tool claude --skill xlsx"
        )
    return None


def _select_install_skills(skills: list[str], *, prune: bool, has_questionary: bool) -> list[str]:
    """Skip the skill prompt for a prune-only invocation."""
    if prune and not skills:
        return []
    return _select_skills(skills, has_questionary=has_questionary)


def _cmd_variables(argv: list[str]) -> int:
    """Print a sorted two-column table of all available {{key}} template variables.

    Calls ``build_template_ctx()`` to resolve current values from config.yaml
    and accounts.yaml, then prints a human-readable table sorted alphabetically
    by variable name.  Always returns 0.

    Flags:
        --json   Output resolved variables as a JSON object {key: value, ...}.
    """
    from fieldkit.skill.template import build_template_ctx

    want_json = "--json" in argv
    ctx = build_template_ctx()

    if want_json:
        # implementation change: JSON output — flat object sorted by key for deterministic output
        _echo(json.dumps(dict(sorted(ctx.items())), indent=2))
        return 0

    _echo(f"{'Variable':<20}  {'Current Value'}")
    _echo("-" * 20 + "  " + "-" * 30)
    for key in sorted(ctx):
        # implementation change: skip deprecated data_repo key (superseded by fieldkit_home)
        if key == "data_repo":
            continue
        # implementation change: show <not set> for blank values
        val = ctx[key]
        display = val if val else "<not set>"
        _echo(f"{key:<20}  {display}")

    return 0


def _cmd_list(argv: list[str]) -> int:
    want_json = "--json" in argv
    verbose = "--verbose" in argv
    group_filter = None
    for i, arg in enumerate(argv):
        if arg == "--group" and i + 1 < len(argv):
            group_filter = argv[i + 1].lower()

    skills = _load_all_skills()

    if group_filter:
        # Filter by name prefix or groups_needed content
        skills = [
            s for s in skills if group_filter in s["name"].lower() or group_filter in s.get("groups_needed", "").lower()
        ]
        # implementation change: emit a clear notice when no skills match the group filter
        if not skills:
            _echo(f"no skills found matching group '{group_filter}'")
            return 0

    if want_json:
        _echo(json.dumps(skills, indent=2))
    else:
        _render_list_human(skills, verbose=verbose)
    return 0


def _cmd_show(argv: list[str]) -> int:
    want_json = "--json" in argv
    names = [a for a in argv if not a.startswith("-")]

    if not names:
        _echo("Usage: fieldkit skill show <skill-name> [--json]", err=True)
        return 1

    skill_name = names[0]
    skills_dir = _skills_dir()

    if not skills_dir.is_dir():
        _echo(f"Skills directory not found: {skills_dir}", err=True)
        _echo(f"Set FIELDKIT_SKILLS_DIR or configure fieldkit_root in {CONFIG_PATH}", err=True)
        return 1

    # Exact match first, then prefix
    candidates = [
        d for d in skills_dir.iterdir() if d.is_dir() and (d.name == skill_name or d.name.startswith(skill_name))
    ]

    if not candidates:
        _echo(f"Skill {skill_name!r} not found.", err=True)
        _echo("Run `fieldkit skill list` to see available skills.", err=True)
        return 1

    if len(candidates) > 1:
        exact = [d for d in candidates if d.name == skill_name]
        candidates = exact if exact else candidates[:1]

    skill = _load_skill(candidates[0])
    if not skill:
        _echo(f"Could not load skill {skill_name!r}.", err=True)
        return 1

    if want_json:
        _echo(json.dumps(skill, indent=2))
    else:
        _render_show_human(skill)
    return 0


# ---------------------------------------------------------------------------
# Entry point (called from __init__.py)
# ---------------------------------------------------------------------------


def run(subcmd: str, argv: list[str]) -> int:
    """Dispatch skill subcommands.

    Note: the ``install`` subcommand is driven by Click options (--tool, --skill,
    --dry-run) via ``fieldkit/skill/cli.py``.  This dispatcher path is kept for
    backward compatibility with any direct callers; it runs non-interactively
    with no pre-selections and no dry-run.  To pass flags, invoke via the Click
    CLI (``fieldkit skill install --tool opencode --skill account-pulse``).
    """
    if subcmd == "list":
        return _cmd_list(argv)
    if subcmd == "show":
        return _cmd_show(argv)
    if subcmd == "install":
        return _cmd_install([], [])
    if subcmd == "variables":
        return _cmd_variables(argv)
    _echo(f"Unknown subcommand: {subcmd!r}", err=True)
    return 1
