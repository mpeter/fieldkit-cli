---
caste: derived
derived_from:
- src/fieldkit/ import graph
- src/fieldkit/skills/
- tach.toml
generated_by: scripts/generate_dep_map.py
---

# Dependency Map

> **Derived document — generated exhaust, not a system of record.** Verify facts against the sources listed in `derived_from`.

> Auto-generated 2026-09-19 from live repo analysis. Do not edit manually.
> Re-generate: `uv run python scripts/generate_dep_map.py`

---

## Architecture Layers

```
src/fieldkit/              ← single package root (src/ layout)
  config/                  ← configuration loader and path helpers
  llm/                     ← LLM inference via Vertex AI
  sf/                      ← Salesforce REST client
  gmail/                   ← Gmail discovery and name resolution
  pursuit/                 ← pursuit models, I/O, MEDDPICC helpers
  ingest/                  ← transcript/audio ingest pipeline
  watch/                   ← watcher infrastructure (status, logging, dedup)
  enrich/                  ← contact enrichment schema (shared dependency)
  contact/                 ← contact discovery, enrichment, and reporting domain
  skill/                   ← skill template rendering
  tasks/                   ← action item classification
  shadowbot/               ← ShadowBot client (domain)
  commands/                ← CLI adapters (thin wrappers over domain)
    sf/                    ← sf subcommands
    gmail/                 ← gmail subcommands
    brief/                 ← brief command
    pipeline/              ← pipeline command
    watch/                 ← watcher daemons
    ingest/                ← ingest pipeline CLI
    contact/               ← contact discovery/enrichment/report CLI
    pursuit/               ← pursuit management CLI
    docs/                  ← docs integration CLI
    shadowbot/             ← ShadowBot CLI
    init/                  ← first-run configuration wizard CLI
    skill/                 ← skill runner CLI
    datasync/              ← ordered full data pipeline runner
    issue/                 ← local issue tracker
    version/               ← version info
  skills/                  ← agent skills (markdown + SKILL.md, not Python)
hooks/                     ← Claude Code hooks + git pre-commit
```

**Boundary enforcement:** ✅ All module boundaries clean (`uvx tach check` passes)

---

## Domain Module Consumer Map

Which `fieldkit.commands.*` packages import which domain modules.
✓ = imported, · = not used.

| Package | config | pursuit | watch | cli exit | llm |
| --- | --- | --- | --- | --- | --- |
| `gmail` | ✓ | · | · | ✓ | · |
| `sf` | ✓ | · | · | ✓ | · |
| `brief` | ✓ | ✓ | · | ✓ | ✓ |
| `pipeline` | ✓ | ✓ | · | ✓ | ✓ |
| `watch` | ✓ | · | ✓ | ✓ | · |
| `ingest` | ✓ | · | · | ✓ | · |
| `contact` | ✓ | · | · | ✓ | · |
| `pursuit` | ✓ | ✓ | · | ✓ | · |
| `meeting` | ✓ | · | · | ✓ | · |
| `shadowbot` | ✓ | · | · | ✓ | · |
| `init` | ✓ | · | · | ✓ | · |
| `skill` | ✓ | · | · | · | · |
| `datasync` | ✓ | · | · | ✓ | · |
| `issue` | ✓ | · | · | ✓ | · |
| `version` | ✓ | · | · | · | · |

---

## Key Domain Modules — Blast Radius

| Module | Consumers | Notes |
| --- | --- | --- |
| `fieldkit.config/` | 63 | Most-imported module; every CLI subpackage and hooks depend on it |
| `fieldkit.cli_exit/` | 63 | Exit code enforcement — used at every CLI entry point |
| `fieldkit.pursuit/` | 5 | Frontmatter models, io, MEDDPICC helpers — used by many subpackages |
| `fieldkit.llm/` | 3 | Vertex AI synthesis — used by AI-driven watchers and morning brief |
| `fieldkit.watch/` | 1 | Watcher status, logging, dedup — used by all watch daemons |

---

## Domain Submodule Detail

For packages where submodule granularity affects blast radius:

### fieldkit.pursuit

| Submodule | External Consumers |
| --- | --- |
| `fieldkit.pursuit.io` | `brief`, `ingest`, `pipeline`, `pursuit`, `sf`, `skill` |
| `fieldkit.pursuit.models` | `pursuit`, `sf` |
| `fieldkit.pursuit.stale` | `brief` |
| `fieldkit.pursuit.utils` | — (domain-internal) |

### fieldkit.gmail

| Submodule | External Consumers |
| --- | --- |
| `fieldkit.gmail.discover` | `brief`, `contact`, `datasync`, `doctor`, `gmail`, `ingest`, `pipeline`, `version` |
| `fieldkit.gmail.names` | `gmail` |


---

## Change Impact Checklist

### Changing a domain module
```bash
# 1. Find all consumers
rg 'from fieldkit.<module> import' src/fieldkit/commands/ hooks/ --type py
# 2. Type-check
uv run mypy src/fieldkit/ hooks/*.py --no-error-summary
# 3. Run tests
uv run pytest tests/ -q
```

### Renaming a CLI command
```bash
# 1. Update _COMMANDS in src/fieldkit/__main__.py
# 2. Search skills
rg 'fieldkit <old-name>' src/fieldkit/skills/ --type md
# 3. Search docs
rg 'fieldkit <old-name>' docs/
```

### Adding a new domain module
```bash
# 1. Add to DOMAIN_MODULES list in scripts/generate_dep_map.py
# 2. Add to tach.toml if the module must be isolated
# 3. Regenerate: make docs
```

## Quick Queries

```bash
# What commands packages use fieldkit.pursuit?
rg 'from fieldkit.pursuit import' src/fieldkit/commands/ hooks/ --type py

# What skills reference the fieldkit-sales MCP group?
rg 'fieldkit-sales' src/fieldkit/skills/ --type md -l

# Verify no boundary violations
uv run tach check
```

