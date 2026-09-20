#!/usr/bin/env python3
"""
generate_dep_map.py — Auto-generate docs/dependency-map.md from live repo analysis.

Run after any structural change that adds/removes modules or MCP group references:
    uv run python scripts/generate_dep_map.py

The output file is committed to the repo. Drift is caught by `make quality` (--check mode).
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT = REPO_ROOT / "docs" / "dependency-map.md"
SRC_ROOT = REPO_ROOT / "src"

sys.path.insert(0, str(SRC_ROOT))
from fieldkit.provenance import derived_doc_banner, derived_doc_marker  # noqa: E402

# Provenance marker — this doc is derived exhaust, not a system of record.
_MARKER = derived_doc_marker(
    caste="derived",
    derived_from=["src/fieldkit/ import graph", "src/fieldkit/skills/", "tach.toml"],
    generated_by="scripts/generate_dep_map.py",
)

# Domain modules in dependency order (foundation → utilities → domain)
DOMAIN_MODULES = [
    "config",
    "pursuit",
    "gmail",
    "watch",
    "cli_exit",
    "sf",
    "llm",
    "skill",
    "enrich",
    "contact",
    "ingest",
    "tasks",
    "config.dotenv",
]

# fieldkit.commands.* subpackages to audit for domain imports
COMMANDS_PACKAGES = [
    "gmail",
    "sf",
    "brief",
    "pipeline",
    "watch",
    "ingest",
    "contact",
    "pursuit",
    "meeting",
    "shadowbot",
    "init",
    "skill",
    "datasync",
    "issue",
    "version",
]

# Domain subpackages where submodule granularity matters for blast radius
SUBMODULE_DETAIL = {
    "pursuit": ["io", "models", "stale", "utils"],
    "gmail": ["discover", "names"],
}


def _rg(pattern: str, path: str, extra: list[str] | None = None) -> list[str]:
    """Search Python files under path for pattern; return relative file paths.

    Pure-Python replacement for ripgrep — no external binary required.
    The ``extra`` argument is accepted for API compatibility but unused
    (all callers pass only --type py / -l flags which are built-in here).
    """
    compiled = re.compile(pattern)
    root = REPO_ROOT / path if not Path(path).is_absolute() else Path(path)
    matches: list[str] = []
    for py_file in root.rglob("*.py"):
        try:
            if compiled.search(py_file.read_text(encoding="utf-8", errors="ignore")):
                matches.append(str(py_file.relative_to(REPO_ROOT)))
        except OSError:
            continue
    return matches


def _rg_count(pattern: str, path: str) -> int:
    return len(_rg(pattern, path))


def _build_import_matrix() -> dict[str, dict[str, bool]]:
    """Map commands_pkg -> domain_module -> True if imported."""
    matrix: dict[str, dict[str, bool]] = {}
    for pkg in COMMANDS_PACKAGES:
        pkg_path = f"src/fieldkit/commands/{pkg}"
        matrix[pkg] = {}
        for mod in DOMAIN_MODULES:
            files = _rg(f"from fieldkit\\.{mod}( import|$)", pkg_path)
            matrix[pkg][mod] = bool(files)
    return matrix


def _tach_status() -> str:
    # Try uvx first (tach installed as a standalone tool), fall back to uv run.
    for cmd in [["uvx", "tach", "check"], ["uv", "run", "tach", "check"]]:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            return "✅ All module boundaries clean (`uvx tach check` passes)"
        # If the command was not found, try the next alternative.
        if result.returncode != 127 and "No such file" not in (result.stderr or ""):
            break
    return f"⚠️ Boundary violations detected:\n```\n{result.stdout.strip()}\n```"


def _domain_consumer_counts() -> dict[str, int]:
    """Return number of Python files importing each domain module."""
    counts: dict[str, int] = {}
    search_roots = [REPO_ROOT / "src" / "fieldkit" / "commands", REPO_ROOT / "hooks"]
    for mod in DOMAIN_MODULES:
        compiled = re.compile(rf"from fieldkit\.{mod}( import|$)", re.MULTILINE)
        matched: set[Path] = set()
        for root in search_roots:
            if not root.exists():
                continue
            for py_file in root.rglob("*.py"):
                try:
                    if compiled.search(py_file.read_text(encoding="utf-8", errors="ignore")):
                        matched.add(py_file)
                except OSError:
                    continue
        counts[mod] = len(matched)
    return counts


def _build_submodule_detail() -> dict[str, dict[str, list[str]]]:
    """Map domain_pkg -> submod -> list of commands packages that import it."""
    detail: dict[str, dict[str, list[str]]] = {}
    for pkg, submods in SUBMODULE_DETAIL.items():
        detail[pkg] = {}
        for sub in submods:
            pattern = f"from fieldkit\\.{pkg}\\.{sub}"
            files = _rg(pattern, "src/fieldkit/commands/")
            consumers = sorted({Path(f).parent.name for f in files})
            detail[pkg][sub] = consumers
    return detail


def generate() -> str:
    """Generate dependency-map.md content."""
    today = date.today().isoformat()
    matrix = _build_import_matrix()
    consumer_counts = _domain_consumer_counts()
    tach_status = _tach_status()

    # Package-existence guard
    for pkg in COMMANDS_PACKAGES:
        pkg_path = REPO_ROOT / "src" / "fieldkit" / "commands" / pkg
        if not any(pkg_path.glob("*.py")):
            print(
                f"WARNING: src/fieldkit/commands/{pkg}/ has no .py files — remove from COMMANDS_PACKAGES",
                file=sys.stderr,
            )

    lines: list[str] = [
        _MARKER.rstrip("\n"),
        "",
        "# Dependency Map",
        "",
        derived_doc_banner(),
        "",
        f"> Auto-generated {today} from live repo analysis. Do not edit manually.",
        "> Re-generate: `uv run python scripts/generate_dep_map.py`",
        "",
        "---",
        "",
        "## Architecture Layers",
        "",
        "```",
        "src/fieldkit/              ← single package root (src/ layout)",
        "  config/                  ← configuration loader and path helpers",
        "  llm/                     ← LLM inference via Vertex AI",
        "  sf/                      ← Salesforce REST client",
        "  gmail/                   ← Gmail discovery and name resolution",
        "  pursuit/                 ← pursuit models, I/O, MEDDPICC helpers",
        "  ingest/                  ← transcript/audio ingest pipeline",
        "  watch/                   ← watcher infrastructure (status, logging, dedup)",
        "  enrich/                  ← contact enrichment schema (shared dependency)",
        "  contact/                 ← contact discovery, enrichment, and reporting domain",
        "  skill/                   ← skill template rendering",
        "  tasks/                   ← action item classification",
        "  shadowbot/               ← ShadowBot client (domain)",
        "  commands/                ← CLI adapters (thin wrappers over domain)",
        "    sf/                    ← sf subcommands",
        "    gmail/                 ← gmail subcommands",
        "    brief/                 ← brief command",
        "    pipeline/              ← pipeline command",
        "    watch/                 ← watcher daemons",
        "    ingest/                ← ingest pipeline CLI",
        "    contact/               ← contact discovery/enrichment/report CLI",
        "    pursuit/               ← pursuit management CLI",
        "    docs/                  ← docs integration CLI",
        "    shadowbot/             ← ShadowBot CLI",
        "    init/                  ← first-run configuration wizard CLI",
        "    skill/                 ← skill runner CLI",
        "    datasync/              ← ordered full data pipeline runner",
        "    issue/                 ← local issue tracker",
        "    version/               ← version info",
        "  skills/                  ← agent skills (markdown + SKILL.md, not Python)",
        "hooks/                     ← Claude Code hooks + git pre-commit",
        "```",
        "",
        f"**Boundary enforcement:** {tach_status}",
        "",
        "---",
        "",
        "## Domain Module Consumer Map",
        "",
        "Which `fieldkit.commands.*` packages import which domain modules.",
        "✓ = imported, · = not used.",
        "",
    ]

    active_mods = [m for m in DOMAIN_MODULES if consumer_counts.get(m, 0) > 0]
    active_pkgs = [p for p in COMMANDS_PACKAGES if any(matrix[p][m] for m in active_mods)]

    mod_labels = [m.replace("_", " ") for m in active_mods]
    header = "| Package | " + " | ".join(mod_labels) + " |"
    sep = "| --- | " + " | ".join(["---"] * len(active_mods)) + " |"
    lines.append(header)
    lines.append(sep)

    for pkg in active_pkgs:
        cells = ["✓" if matrix[pkg][mod] else "·" for mod in active_mods]
        lines.append(f"| `{pkg}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        "---",
        "",
        "## Key Domain Modules — Blast Radius",
        "",
        "| Module | Consumers | Notes |",
        "| --- | --- | --- |",
    ]

    blast_notes: dict[str, str] = {
        "config": "Most-imported module; every CLI subpackage and hooks depend on it",
        "pursuit": "Frontmatter models, io, MEDDPICC helpers — used by many subpackages",
        "gmail": "Thread discovery, DB path, name resolution — used by all Gmail commands",
        "watch": "Watcher status, logging, dedup — used by all watch daemons",
        "cli_exit": "Exit code enforcement — used at every CLI entry point",
        "sf": "Salesforce REST client — primary consumer: commands/sf",
        "llm": "Vertex AI synthesis — used by AI-driven watchers and morning brief",
        "skill": "Skill template rendering — used by commands/skill",
        "enrich": "Contact enrichment schema (ContactRecord) — shared by commands/contact",
        "contact": "Contact discovery, enrichment, and reporting domain — used by commands/contact",
        "ingest": "Ingest pipeline domain — used by commands/ingest",
        "tasks": "Action item classification — used by ingest pipeline",
        "config.dotenv": "Environment loading shim",
    }

    for mod in sorted(active_mods, key=lambda m: -consumer_counts.get(m, 0)):
        count = consumer_counts.get(mod, 0)
        note = blast_notes.get(mod, "")
        mod_label = f"fieldkit.{mod}/"
        lines.append(f"| `{mod_label}` | {count} | {note} |")

    submodule_detail = _build_submodule_detail()
    lines += ["", "---", "", "## Domain Submodule Detail", ""]
    lines += ["For packages where submodule granularity affects blast radius:", ""]
    for pkg, submods in submodule_detail.items():
        lines += [f"### fieldkit.{pkg}", ""]
        lines += ["| Submodule | External Consumers |", "| --- | --- |"]
        for sub, consumers in submods.items():
            consumer_str = ", ".join(f"`{c}`" for c in consumers) if consumers else "— (domain-internal)"
            lines.append(f"| `fieldkit.{pkg}.{sub}` | {consumer_str} |")
        lines.append("")

    lines += [
        "",
        "---",
        "",
        "## Change Impact Checklist",
        "",
        "### Changing a domain module",
        "```bash",
        "# 1. Find all consumers",
        "rg 'from fieldkit.<module> import' src/fieldkit/commands/ hooks/ --type py",
        "# 2. Type-check",
        "uv run mypy src/fieldkit/ hooks/*.py --no-error-summary",
        "# 3. Run tests",
        "uv run pytest tests/ -q",
        "```",
        "",
        "### Renaming a CLI command",
        "```bash",
        "# 1. Update _COMMANDS in src/fieldkit/__main__.py",
        "# 2. Search skills",
        "rg 'fieldkit <old-name>' src/fieldkit/skills/ --type md",
        "# 3. Search docs",
        "rg 'fieldkit <old-name>' docs/",
        "```",
        "",
        "### Adding a new domain module",
        "```bash",
        "# 1. Add to DOMAIN_MODULES list in scripts/generate_dep_map.py",
        "# 2. Add to tach.toml if the module must be isolated",
        "# 3. Regenerate: make docs",
        "```",
        "",
        "## Quick Queries",
        "",
        "```bash",
        "# What commands packages use fieldkit.pursuit?",
        "rg 'from fieldkit.pursuit import' src/fieldkit/commands/ hooks/ --type py",
        "",
        "# What skills reference the fieldkit-sales MCP group?",
        "rg 'fieldkit-sales' src/fieldkit/skills/ --type md -l",
        "",
        "# Verify no boundary violations",
        "uv run tach check",
        "```",
        "",
    ]

    return "\n".join(lines) + "\n"


def _source_mtime() -> float:
    """Return the newest mtime across all Python source files and this script."""
    candidates = list(SRC_ROOT.rglob("*.py"))
    candidates.append(Path(__file__))
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
            print("ERROR: docs/dependency-map.md does not exist — run 'make docs'", file=sys.stderr)
            sys.exit(1)
        # Fast path: skip generation if output is newer than all sources.
        if OUTPUT.stat().st_mtime > _source_mtime():
            print("docs/dependency-map.md is up to date ✓", file=sys.stderr)
            sys.exit(0)

    print("Generating dependency map...", file=sys.stderr)
    content = generate()

    if check_mode:
        existing = _normalize_generated_date(OUTPUT.read_text(encoding="utf-8"))
        content = _normalize_generated_date(content)
        if existing != content:
            import difflib

            diff = list(
                difflib.unified_diff(
                    existing.splitlines(),
                    content.splitlines(),
                    fromfile="committed",
                    tofile="generated",
                    lineterm="",
                )
            )
            print("\n".join(diff[:40]), file=sys.stderr)
            if len(diff) > 40:
                print(f"... and {len(diff) - 40} more lines", file=sys.stderr)
            print("ERROR: docs/dependency-map.md is stale — run 'make docs' to regenerate", file=sys.stderr)
            sys.exit(1)
        print("docs/dependency-map.md is up to date ✓", file=sys.stderr)
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8")
