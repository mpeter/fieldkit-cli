"""fieldkit skill CLI group — skill discovery and inspection.

Subcommands:
  list            List all available skills with names and trigger descriptions
  install         Install skills into project-local paths for the selected AI tools
  variables       Show all available {{key}} template variables and their current values
  show            Show full details for a specific skill
  eval            Run static and behavioral assertions against skill definitions
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.config import llm_disabled
from fieldkit.config.optional_dependencies import LLM_IMPORT_ROOTS, require_optional_profile


def _require_live_eval_profile(behavioral: bool, calibrate: bool, routing: bool) -> None:
    """Require LLM dependencies only for live evaluator modes."""
    if (behavioral or calibrate or routing) and not llm_disabled():
        require_optional_profile("skill eval", "llm", LLM_IMPORT_ROOTS)


@click.group(
    name="skill",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Skill discovery and inspection."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)


def _list_argv(*, as_json: bool, group: str | None, verbose: bool) -> list[str]:
    argv: list[str] = []
    if as_json:
        argv.append("--json")
    if group:
        argv.extend(["--group", group])
    if verbose:
        argv.append("--verbose")
    return argv


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.option("--group", "group", default=None, help="Filter skills by group name.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Show full skill descriptions.")
def cmd_list(as_json: bool, group: str | None, verbose: bool) -> None:
    """List all available skills with names and trigger descriptions."""
    from fieldkit.commands.skill import _runner

    raise SystemExit(_runner._cmd_list(_list_argv(as_json=as_json, group=group, verbose=verbose)))


@declare_write("workspace")
@cli.command("install")
@click.option(
    "--tool",
    "tools",
    multiple=True,
    metavar="TOOL",
    help="Pre-select tool(s): opencode, claude-code, cursor, gemini (repeatable). Skips the tool selection prompt.",
)
@click.option(
    "--skill",
    "skills",
    multiple=True,
    metavar="SKILL",
    help="Pre-select a skill by name (repeatable). Skips the skill selection prompt.",
)
@click.option(
    "--all", "install_all", is_flag=True, default=False, help="Install all skills without interactive selection."
)
@click.option(
    "--global",
    "global_install",
    is_flag=True,
    default=False,
    help="Install handoffs or pickup into a registered OpenCode or Claude Code user skill root.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview install without writing files.")
@click.option("--prune", is_flag=True, default=False, help="Preview removal of stale installer-owned skills.")
@click.option("--confirm", is_flag=True, default=False, help="Apply removals selected by --prune.")
@click.option("--force", is_flag=True, default=False, help="Overwrite locally modified or untrusted skill targets.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the installation result as JSON.")
def cmd_install(
    tools: tuple[str, ...],
    skills: tuple[str, ...],
    install_all: bool,
    global_install: bool,
    dry_run: bool,
    prune: bool,
    confirm: bool,
    force: bool,
    as_json: bool,
) -> None:
    """Install skills into project-local paths for the selected AI tools.

    Scans the current directory for known AI tool config directories
    (.opencode/, .claude/, .cursor/, .gemini/) and presents an interactive
    multi-select prompt for tools and skills.

    Pass --tool and --skill to skip the interactive prompts (useful for CI).
    Pass --all to install every skill without selecting (useful for make install).
    Pass --global with an explicit OpenCode or Claude Code tool and handoffs or
    pickup skill to install it for the current user.
    Pass --prune to preview stale installer-owned skills, then add --confirm to
    remove them. Untracked local skills are always left untouched.
    """
    from fieldkit.commands.skill import _runner
    from fieldkit.commands.skill._output import json_output

    if as_json and (not tools or (not skills and not install_all and not prune)):
        raise click.UsageError("--json requires --tool and --skill, --all, or --prune to avoid interactive prompts")

    with json_output(as_json):
        raise SystemExit(
            _runner._cmd_install(
                list(tools),
                list(skills),
                install_all=install_all,
                global_install=global_install,
                dry_run=dry_run,
                prune=prune,
                confirm=confirm,
                force=force,
            )
        )


@cli.command("variables")
@click.argument("skill_name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_variables(skill_name: str | None, as_json: bool) -> None:
    """Show all available {{key}} template variables and their current values.

    Template variables are global — they apply to all skills.
    Passing SKILL_NAME is accepted but has no effect on the output.
    """
    from fieldkit.commands.skill import _runner

    if skill_name is not None:
        click.echo(
            f"Note: template variables are global and not skill-specific. "
            f"Showing all variables (SKILL_NAME={skill_name!r} ignored).",
            err=True,
        )
    argv: list[str] = []
    if as_json:
        argv.append("--json")
    raise SystemExit(_runner._cmd_variables(argv))


@cli.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_show(name: str, as_json: bool) -> None:
    """Show full details for a specific skill."""
    from fieldkit.commands.skill import _runner

    argv: list[str] = [name]
    if as_json:
        argv.append("--json")
    raise SystemExit(_runner._cmd_show(argv))


def _dispatch_eval(
    *,
    skill_names: list[str],
    run_all: bool,
    failed: bool,
    no_behavioral: bool,
    as_json: bool,
    scaffold_name: str | None,
    behavioral: bool,
    calibrate: bool,
    routing: bool,
    limit: int | None,
) -> int:
    """Dispatch the routing mode or the established skill evaluator."""
    _require_live_eval_profile(behavioral, calibrate, routing)
    if routing:
        if (
            behavioral
            or calibrate
            or run_all
            or skill_names
            or failed
            or no_behavioral
            or scaffold_name is not None
            or limit is not None
        ):
            raise click.UsageError("--routing cannot be combined with other evaluation modes or selectors")
        from fieldkit.commands.skill.routing_eval import run_routing_eval

        return run_routing_eval(json_output=as_json, model=None)

    from fieldkit.commands.skill.eval_runner import run_eval_cmd

    return run_eval_cmd(
        skill_names=skill_names,
        run_all=run_all,
        failed_only=failed,
        json_output=as_json,
        no_behavioral=no_behavioral,
        scaffold_name=scaffold_name,
        behavioral=behavioral,
        calibrate=calibrate,
        limit=limit,
    )


@cli.command("eval")
@click.argument("names", nargs=-1)
@click.option("--all", "run_all", is_flag=True, default=False, help="Evaluate all skills.")
@click.option("--failed", is_flag=True, default=False, help="Show only skills with failing checks.")
@click.option("--no-behavioral", is_flag=True, default=False, help="Skip printing behavioral eval cases.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
@click.option(
    "--scaffold",
    "scaffold_name",
    default=None,
    metavar="SKILL",
    help="Scaffold a starter evals.json for the named skill.",
)
@click.option("--behavioral", is_flag=True, default=False, help="Run LLM-graded behavioral evals.")
@click.option(
    "--calibrate",
    is_flag=True,
    default=False,
    help="Run calibration fixtures to validate judge accuracy.",
)
@click.option("--routing", is_flag=True, default=False, help="Evaluate utterance routing against all skills.")
@click.option(
    "--limit",
    default=None,
    type=int,
    metavar="N",
    help="Max number of skills to judge (behavioral mode).",
)
@click.option(
    "--skill",
    "skill_flags",
    multiple=True,
    metavar="NAME",
    help="Skill name to evaluate (repeatable; additive with positional names).",
)
def cmd_eval(
    names: tuple[str, ...],
    run_all: bool,
    failed: bool,
    no_behavioral: bool,
    as_json: bool,
    scaffold_name: str | None,
    behavioral: bool,
    calibrate: bool,
    routing: bool,
    limit: int | None,
    skill_flags: tuple[str, ...],
) -> None:
    """Run static assertions and print behavioral eval cases for skill(s).

    Use --behavioral to run LLM-graded coverage evals against skill assertions.
    Use --calibrate to validate judge accuracy against gold-standard fixtures.
    Use --routing to evaluate utterance-to-skill selection against the full corpus.
    """
    # --skill values precede bare positional names; order is significant because
    # --limit truncates the list in behavioral mode.
    raise SystemExit(
        _dispatch_eval(
            skill_names=list(skill_flags) + list(names),
            run_all=run_all,
            failed=failed,
            as_json=as_json,
            no_behavioral=no_behavioral,
            scaffold_name=scaffold_name,
            behavioral=behavioral,
            calibrate=calibrate,
            routing=routing,
            limit=limit,
        )
    )
