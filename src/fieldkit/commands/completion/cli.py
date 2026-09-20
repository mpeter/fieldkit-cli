"""fieldkit completion — shell completion script generator.

Outputs the shell completion script for bash, zsh, or fish.

Usage:
    fieldkit completion bash   → bash completion script
    fieldkit completion zsh    → zsh completion script
    fieldkit completion fish   → fish completion script

To activate in bash:
    eval "$(fieldkit completion bash)"

Or add to ~/.bashrc:
    source <(fieldkit completion bash)

To activate in zsh (add to ~/.zshrc):
    eval "$(fieldkit completion zsh)"

To activate in fish (add to ~/.config/fish/completions/fieldkit.fish):
    fieldkit completion fish | source
"""

import os
import subprocess
import sys

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import TIMEOUT_HEALTH_CHECK


@click.group(
    name="completion",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]), required=False)
@click.pass_context
def cli(ctx: click.Context, shell: str | None) -> None:
    """Output shell completion script.

    Supported shells: bash, zsh, fish.

    \b
    Quick setup:
        eval "$(fieldkit completion bash)"   # add to ~/.bashrc
        eval "$(fieldkit completion zsh)"    # add to ~/.zshrc
        fieldkit completion fish | source    # fish

    Uses Click's built-in completion mechanism. The generated script sets
    _FIELDKIT_COMPLETE=<shell>_source when sourced, which triggers Click to
    output completions on subsequent invocations.
    """
    if ctx.invoked_subcommand is not None:
        return

    if shell is None:
        click.echo(ctx.get_help())
        raise SystemExit(EXIT_DATA) from None

    env_var = "_FIELDKIT_COMPLETE"
    source_cmd = f"{shell}_source"

    env = {**os.environ, env_var: source_cmd}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fieldkit"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_HEALTH_CHECK,
        )
        if result.returncode not in (0, 1):
            # Click completion exits 1 on some shells — that's expected
            click.echo(
                f"Warning: completion script exited with code {result.returncode}",
                err=True,
            )
        output = result.stdout
        if not output:
            # Fall back to shell-specific script
            output = _generate_completion_script(shell)
        click.echo(output, nl=False)
    except OSError as exc:
        click.echo(f"Error generating completion script: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None


def _generate_completion_script(shell: str) -> str:
    """Generate shell-specific completion script."""
    completions = {
        "bash": _bash_script,
        "zsh": _zsh_script,
        "fish": _fish_script,
    }
    return completions[shell]()


def _bash_script() -> str:
    return """\
_fieldkit_completion() {
    local IFS=$'\\n'
    local response
    response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD _FIELDKIT_COMPLETE=bash_complete fieldkit 2>/dev/null)
    for completion in $response; do
        IFS=',' read -r type value <<< "$completion"
        if [[ $type == 'dir' ]]; then
            COMPREPLY=()
            compopt -o dirnames
        elif [[ $type == 'file' ]]; then
            COMPREPLY=()
            compopt -o default
        elif [[ $type == 'plain' ]]; then
            COMPREPLY+=($value)
        fi
    done
    return 0
}

complete -o nosort -F _fieldkit_completion fieldkit
"""


def _zsh_script() -> str:
    return """\
#compdef fieldkit

_fieldkit_completion() {
    local -a completions
    local -a completions_with_descriptions
    local -a response
    (( ! $+commands[fieldkit] )) && return 1

    response=("${(@f)$(env COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) _FIELDKIT_COMPLETE=zsh_complete fieldkit 2>/dev/null)}")

    for type key descr in ${response}; do
        if [[ "$type" == "plain" ]]; then
            if [[ "$descr" == "_" ]]; then
                completions+=("$key")
            else
                completions_with_descriptions+=("$key":"$descr")
            fi
        elif [[ "$type" == "dir" ]]; then
            _path_files -/
        elif [[ "$type" == "file" ]]; then
            _path_files -f
        fi
    done

    if [ -n "$completions_with_descriptions" ]; then
        _describe -V unsorted completions_with_descriptions -U
    fi

    if [ -n "$completions" ]; then
        compadd -U -V unsorted -a completions
    fi
}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    _fieldkit_completion "$@"
else
    compdef _fieldkit_completion fieldkit
fi
"""


def _fish_script() -> str:
    return """\
function _fieldkit_completion
    set -l response (env _FIELDKIT_COMPLETE=fish_complete COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) fieldkit 2>/dev/null)

    for completion in $response
        set -l metadata (string split "," $completion)

        if test $metadata[1] = "dir"
            __fish_complete_directories $metadata[2]
        else if test $metadata[1] = "file"
            __fish_complete_path $metadata[2]
        else if test $metadata[1] = "plain"
            echo $metadata[2]
        end
    end
end

complete --no-files --command fieldkit --arguments "(_fieldkit_completion)"
"""
