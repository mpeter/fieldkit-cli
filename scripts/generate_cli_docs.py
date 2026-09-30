#!/usr/bin/env python3
"""
generate_cli_docs.py — Auto-generate docs/cli-reference.md by in-process Click introspection.

Run after any CLI change that adds/removes/modifies commands or options:
    uv run python scripts/generate_cli_docs.py

The output file is committed to the repo. Any drift between the code and the
doc is caught by `make quality` (which runs this script and diffs the result).

The shared `fieldkit.cli_registry.walk_cli()` supplies every command node.
Task and authentication guidance lives in the linked user guides, not in this
generated reference. Check mode always compares rendered content with the file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT = REPO_ROOT / "docs" / "cli-reference.md"

sys.path.insert(0, str(REPO_ROOT / "src"))
from fieldkit.__main__ import _COMMANDS as _COMMANDS_DICT  # noqa: E402
from fieldkit.cli_registry import CommandNode, walk_cli  # noqa: E402
from fieldkit.provenance import derived_doc_banner, derived_doc_marker  # noqa: E402

# Help is wrapped at a fixed width so the output does not depend on the terminal
# the generator happened to run in. The subprocess version inherited COLUMNS from
# the caller's environment, so the committed doc could change wrapping purely
# because of who ran `make docs` — a diff the freshness gate would then report as
# staleness. 78 reproduces the width Click used when its stdout was a pipe
# (min(80, max_content_width) - 2), keeping this rewrite byte-identical to the
# doc it replaces.
_DOC_WIDTH = 78

# Provenance marker — this doc is derived exhaust, not a system of record.
_MARKER = derived_doc_marker(
    caste="derived",
    derived_from=["fieldkit --help (live CLI surface)"],
    generated_by="scripts/generate_cli_docs.py",
)

_PREFERRED_ORDER = [
    "auth",
    "sf",
    "gmail",
    "pursuit",
    "shadowbot",
    "watch",
    "ingest",
    "sync",
    "issue",
    "skill",
    "init",
    "brief",
    "pipeline",
    "version",
]


def command_groups() -> list[str]:
    """Order the current registered groups without copying the command inventory."""
    return [g for g in _PREFERRED_ORDER if g in _COMMANDS_DICT] + sorted(
        g for g in _COMMANDS_DICT if g not in _PREFERRED_ORDER
    )


def render_help(node: CommandNode) -> str:
    """Render a node's `--help` exactly as the CLI would, at a fixed width.

    `terminal_width` is set rather than left to Click's default, which consults
    `shutil.get_terminal_size()` and the `COLUMNS` environment variable — both
    properties of whoever ran the generator, not of the CLI being documented.
    """
    node.ctx.terminal_width = _DOC_WIDTH
    return "\n".join(line.rstrip() for line in node.command.get_help(node.ctx).strip().splitlines())


def child_names(nodes: list[CommandNode], parent_full_name: str) -> list[str]:
    """Direct children of *parent_full_name*, in walk order.

    Read off the shared walk instead of regexing the `Commands:` block out of
    rendered help, which is what the subprocess version did — that parser broke
    on any command whose help happened to contain a two-space-indented line.
    """
    prefix = f"{parent_full_name} "
    return [
        node.full_name[len(prefix) :]
        for node in nodes
        if node.full_name.startswith(prefix) and " " not in node.full_name[len(prefix) :]
    ]


def generate() -> str:
    lines = [
        _MARKER.rstrip("\n"),
        "",
        "# fieldkit CLI Reference",
        "",
        derived_doc_banner(),
        "",
        "> Generated from the registered CLI command tree. Do not edit manually.",
        "> Re-generate: `uv run python scripts/generate_cli_docs.py`",
        "",
        "For setup and workflows, see the [user guide](user-guide.md),",
        "[Salesforce authentication](guides/salesforce-auth.md),",
        "[ShadowBot authentication](guides/shadowbot-auth.md), and",
        "[pipeline workflow](guides/pipeline-workflow.md).",
        "",
        "## Quick reference",
        "",
        "| Group | Key subcommands |",
        "|---|---|",
    ]

    # One walk feeds both the quick-reference table and the per-group sections.
    # The subprocess version walked twice — once for each — which is why every
    # group's --help was spawned twice per run.
    groups = command_groups()
    nodes = walk_cli(groups)

    for group in groups:
        subs = child_names(nodes, group)
        lines.append(f"| `fieldkit {group}` | {', '.join(f'`{s}`' for s in subs)} |")

    lines += ["", "---", ""]

    # Full per-group sections. Nodes come back in depth-first walk order, so
    # emitting them in sequence yields group → subcommand → nested subcommand,
    # with the heading level following the node's depth.
    for node in nodes:
        heading = "#" * (node.depth + 2)
        lines += [f"{heading} `fieldkit {node.full_name}`", "", "```", render_help(node), "```", ""]

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Generate the reference or verify its content without modifying it."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--check", action="store_true")
    check_mode = parser.parse_args(argv).check

    if check_mode and not OUTPUT.exists():
        print("ERROR: docs/cli-reference.md does not exist — run 'make docs'", file=sys.stderr)
        return 1

    print("Generating CLI reference...", file=sys.stderr)
    content = generate()

    if check_mode:
        existing = OUTPUT.read_text(encoding="utf-8")
        if existing != content:
            # Show which lines changed
            import difflib

            diff = list(
                difflib.unified_diff(
                    existing.splitlines(), content.splitlines(), fromfile="committed", tofile="generated", lineterm=""
                )
            )
            print("\n".join(diff[:40]), file=sys.stderr)
            if len(diff) > 40:
                print(f"... and {len(diff) - 40} more lines", file=sys.stderr)
            print("ERROR: docs/cli-reference.md is stale — run 'make docs' to regenerate", file=sys.stderr)
            return 1
        print("docs/cli-reference.md is up to date ✓", file=sys.stderr)
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8")
        print(f"Written to {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
