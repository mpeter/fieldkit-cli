#!/usr/bin/env python3
"""
generate_cli_docs.py — Auto-generate docs/cli-reference.md by in-process Click introspection.

Run after any CLI change that adds/removes/modifies commands or options:
    uv run python scripts/generate_cli_docs.py

The output file is committed to the repo. Any drift between the code and the
doc is caught by `make quality` (which runs this script and diffs the result).

This is the second consumer of `fieldkit.cli_registry.walk_cli()` — D4's "one
mechanism, two consumers". `fieldkit commands --json` reads the leaves of that
same walk; this renders help for every node of it. A second, private traversal
here would drift from the registry silently, and the drift would surface as
documentation that disagrees with the machine-readable surface agents route on.

It previously shelled out to `fieldkit <group> [<sub>] --help`, ~111 spawns each
paying full interpreter and import-graph startup, with the group-help loop run
twice per group. That cost ~25s. In-process it is ~1s.
"""

from __future__ import annotations

import re
import sys
from datetime import date
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
    "docs",
    "watch",
    "enrich",
    "ingest",
    "sync",
    "issue",
    "skill",
    "init",
    "brief",
    "pipeline",
    "version",
]
GROUPS = [g for g in _PREFERRED_ORDER if g in _COMMANDS_DICT] + sorted(
    g for g in _COMMANDS_DICT if g not in _PREFERRED_ORDER
)

# Auth/session notes injected after specific sections
AUTH_NOTES = {
    "sf": """
> **Auth:** Salesforce uses the `sid` session cookie.
> - Authenticate: `fieldkit auth sf` (or use `--sid-file PATH` for an owner-only secret file)
> - Get `sid`: Chrome DevTools → Application → Cookies → your configured `my.salesforce.com` host
> - Check validity: `fieldkit sf session-check`
> - `set-next-steps` and `set-field` are **dry-run by default** — pass `--confirm` to write.
""",
    "shadowbot": """
> **Auth:** ShadowBot uses silent Chrome-cookie OIDC auth (Linux; requires `fieldkit-cli[chrome-auth]`).
> - **Primary:** Chrome Default profile must be logged into your configured ShadowBot host.
> - **Fallback (Linux/SSH/headless):** `fieldkit auth shadowbot --refresh-token-file PATH`
>   Get JWT: Chrome DevTools → Network → filter `openid-connect/token` → Response → `refresh_token`
> - **Config override:** `shadowbot.chrome_cookies_path` in `~/.config/fieldkit/config.yaml`
> - **Session expiry:** determined by the configured identity provider; recover by logging in again.
""",
}


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
        f"> Auto-generated {date.today()} from `fieldkit --help`. Do not edit manually.",
        "> Re-generate: `uv run python scripts/generate_cli_docs.py`",
        "",
        "## Quick reference",
        "",
        "| Group | Key subcommands |",
        "|---|---|",
    ]

    # One walk feeds both the quick-reference table and the per-group sections.
    # The subprocess version walked twice — once for each — which is why every
    # group's --help was spawned twice per run.
    nodes = walk_cli(GROUPS)

    for group in GROUPS:
        subs = child_names(nodes, group)
        lines.append(f"| `fieldkit {group}` | {', '.join(f'`{s}`' for s in subs)} |")

    lines += ["", "---", ""]

    # Full per-group sections. Nodes come back in depth-first walk order, so
    # emitting them in sequence yields group → subcommand → nested subcommand,
    # with the heading level following the node's depth.
    for node in nodes:
        heading = "#" * (node.depth + 2)
        lines += [f"{heading} `fieldkit {node.full_name}`", "", "```", render_help(node), "```", ""]

        if node.depth == 0 and node.full_name in AUTH_NOTES:
            lines.append(AUTH_NOTES[node.full_name])

    lines += [
        "---",
        "",
        "## Common patterns",
        "",
        "### Update SF Next Steps",
        "```bash",
        'fieldkit sf set-next-steps <OPP_ID> "Next steps text here" --confirm',
        "```",
        "",
        "### Write any other approved SF field",
        "```bash",
        "fieldkit sf set-field --list-fields          # see what's allowed",
        'fieldkit sf set-field <OPP_ID> Next_Steps__c "text" --confirm',
        'fieldkit sf set-field <QUOTE_ID> Approval_Comments__c "justification" --sobject SBQQ__Quote__c --confirm',
        "```",
        "",
        "### Refresh SF auth",
        "```bash",
        "# 1. Get sid from Chrome DevTools → your my.salesforce.com host → Cookies → sid",
        "fieldkit auth sf",
        "fieldkit sf session-check",
        "```",
        "",
        "### Refresh ShadowBot auth",
        "```bash",
        "# Primary: Chrome Default profile must be logged into your configured ShadowBot host",
        "fieldkit auth shadowbot   # checks auth status; auto-refreshes via Chrome cookies (Linux)",
        "",
        "# Fallback (SSH/headless/macOS): inject refresh token from DevTools",
        "# Chrome DevTools → Network → filter openid-connect/token → Response → refresh_token",
        "fieldkit auth shadowbot --refresh-token-file PATH",
        "fieldkit shadowbot query 'test query'",
        "```",
        "",
        "### Daily data refresh",
        "```bash",
        "fieldkit gmail sync",
        "fieldkit gmail account-tags",
        "fieldkit gmail enrich-pursuits",
        "fieldkit sf listview",
        "fieldkit watch run backstory-health",
        "fieldkit watch run pursuit-stalls",
        "fieldkit watch run slack-threads",
        "fieldkit brief generate",
        "```",
        "",
        "### Pipeline review",
        "```bash",
        "fieldkit pursuit health",
        "fieldkit pursuit forecast",
        "fieldkit pursuit audit",
        "```",
    ]

    return "\n".join(lines) + "\n"


def _source_mtime() -> float:
    """Return the newest mtime across all CLI source files."""
    candidates = list((REPO_ROOT / "src" / "fieldkit").rglob("*.py"))
    candidates += [REPO_ROOT / "scripts" / "generate_cli_docs.py"]
    return max((p.stat().st_mtime for p in candidates if p.exists()), default=0.0)


def _normalize_generated_date(text: str) -> str:
    """Replace the generation date in the header with a fixed placeholder.

    The header embeds the date the doc was written, which made --check
    time-dependent: any PR built the day after the last regeneration failed
    the freshness gate on a pure date mismatch (no content change).
    """
    return re.sub(r"^> Auto-generated \d{4}-\d{2}-\d{2} ", "> Auto-generated DATE ", text, count=1, flags=re.M)


if __name__ == "__main__":
    check_mode = "--check" in sys.argv

    if check_mode:
        if not OUTPUT.exists():
            print("ERROR: docs/cli-reference.md does not exist — run 'make docs'", file=sys.stderr)
            sys.exit(1)
        # Fast path: skip full generation if output is newer than all sources.
        if OUTPUT.stat().st_mtime > _source_mtime():
            print("docs/cli-reference.md is up to date ✓", file=sys.stderr)
            sys.exit(0)

    print("Generating CLI reference...", file=sys.stderr)
    content = generate()

    if check_mode:
        existing = _normalize_generated_date(OUTPUT.read_text())
        content = _normalize_generated_date(content)
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
            sys.exit(1)
        print("docs/cli-reference.md is up to date ✓", file=sys.stderr)
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content)
        print(f"Written to {OUTPUT}", file=sys.stderr)
