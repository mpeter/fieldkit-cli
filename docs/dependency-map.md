---
caste: derived
derived_from:
- src/fieldkit/
- hooks/
- src/fieldkit/__main__.py
- tach.toml
generated_by: scripts/generate_dep_map.py
---

# fieldkit dependency map

> **Derived document — generated exhaust, not a system of record.** Verify facts against the sources listed in `derived_from`.

This reference is derived from Python source, the public command registry, and
Tach configuration. Source files are parsed for graph discovery, not imported.
This reference does not claim that a quality gate passed.
The contributor gate verifies the declared architecture boundaries.

## Public command adapters

The registry determines public command names. Import areas below include direct
imports from the adapter package and its descendants; they are not runtime traces.

| Command | Registered module | Imported fieldkit areas |
| --- | --- | --- |
| `fieldkit auth` | `fieldkit.commands.auth.cli` | `fieldkit.backstory`, `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.protected_input`, `fieldkit.shadowbot`, `fieldkit.util` |
| `fieldkit autonomy` | `fieldkit.commands.autonomy.cli` | `fieldkit.autonomy`, `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.config`, `fieldkit.driver` |
| `fieldkit brief` | `fieldkit.commands.brief.cli` | `fieldkit.brief`, `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit commands` | `fieldkit.commands.commands.cli` | `fieldkit.cli_registry` |
| `fieldkit companion` | `fieldkit.commands.companion.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.llm` |
| `fieldkit completion` | `fieldkit.commands.completion.cli` | `fieldkit.cli_exit`, `fieldkit.config` |
| `fieldkit contact` | `fieldkit.commands.contact.cli` | `fieldkit.cli_exit`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.contact`, `fieldkit.gmail` |
| `fieldkit doctor` | `fieldkit.commands.doctor.cli` | `fieldkit.cli_exit`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.google_oauth`, `fieldkit.shadowbot` |
| `fieldkit driver` | `fieldkit.commands.driver.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.driver`, `fieldkit.errors` |
| `fieldkit gmail` | `fieldkit.commands.gmail.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.pursuit`, `fieldkit.util` |
| `fieldkit golive` | `fieldkit.commands.golive.cli` | `fieldkit.cli_exit`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.sf` |
| `fieldkit gtask` | `fieldkit.commands.gtask.cli` | `fieldkit.cli_registry`, `fieldkit.gtask` |
| `fieldkit health` | `fieldkit.commands.health.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.config`, `fieldkit.health`, `fieldkit.issue` |
| `fieldkit ingest` | `fieldkit.commands.ingest.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.ingest`, `fieldkit.pursuit`, `fieldkit.tasks`, `fieldkit.util` |
| `fieldkit init` | `fieldkit.commands.init.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.util` |
| `fieldkit issue` | `fieldkit.commands.issue.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.config`, `fieldkit.issue`, `fieldkit.util` |
| `fieldkit meeting` | `fieldkit.commands.meeting.cli` | `fieldkit.cli_exit`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.meeting` |
| `fieldkit pipeline` | `fieldkit.commands.pipeline.cli` | `fieldkit.cli_exit`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.pipeline`, `fieldkit.quota_period`, `fieldkit.sf`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit pursuit` | `fieldkit.commands.pursuit.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.ingest`, `fieldkit.pursuit`, `fieldkit.sf`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit sf` | `fieldkit.commands.sf.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.pursuit`, `fieldkit.sf` |
| `fieldkit shadowbot` | `fieldkit.commands.shadowbot.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.config`, `fieldkit.shadowbot` |
| `fieldkit skill` | `fieldkit.commands.skill.cli` | `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.pursuit`, `fieldkit.skill`, `fieldkit.util` |
| `fieldkit sync` | `fieldkit.commands.datasync.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.contact`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit version` | `fieldkit.commands.version.cli` | `fieldkit.__main__`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.gmail` |
| `fieldkit watch` | `fieldkit.commands.watch.cli` | `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.commands`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.watch` |
| `fieldkit web` | `fieldkit.commands.web.cli` | `fieldkit.cli_exit`, `fieldkit.errors`, `fieldkit.web` |

## Observed cross-area imports

Each area is an immediate fieldkit package/module or the hooks directory.
All Python import statements are inspected, including conditional and type-checking
imports. Same-area imports are omitted; dynamic imports are not inferred.
An empty row means no cross-area fieldkit import was observed, not that runtime
coupling is impossible.

| Source area | Imported fieldkit areas |
| --- | --- |
| `fieldkit` | None observed |
| `fieldkit.__main__` | `fieldkit.cli_exit`, `fieldkit.config` |
| `fieldkit._data` | None observed |
| `fieldkit._sqlite_publication_storage` | `fieldkit.util` |
| `fieldkit._sqlite_snapshot_worker` | `fieldkit.sqlite_read` |
| `fieldkit.autonomy` | `fieldkit.driver` |
| `fieldkit.backstory` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.brief` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.llm`, `fieldkit.pipeline`, `fieldkit.provenance`, `fieldkit.pursuit`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit.circuit_breaker` | None observed |
| `fieldkit.cli_exit` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.sf` |
| `fieldkit.cli_registry` | `fieldkit.__main__`, `fieldkit.config` |
| `fieldkit.commands` | `fieldkit.__main__`, `fieldkit._sqlite_snapshot_worker`, `fieldkit.autonomy`, `fieldkit.backstory`, `fieldkit.brief`, `fieldkit.cli_exit`, `fieldkit.cli_registry`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.contact`, `fieldkit.driver`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.google_oauth`, `fieldkit.gtask`, `fieldkit.health`, `fieldkit.ingest`, `fieldkit.issue`, `fieldkit.llm`, `fieldkit.meeting`, `fieldkit.pipeline`, `fieldkit.protected_input`, `fieldkit.pursuit`, `fieldkit.quota_period`, `fieldkit.sf`, `fieldkit.shadowbot`, `fieldkit.skill`, `fieldkit.tasks`, `fieldkit.util`, `fieldkit.watch`, `fieldkit.web` |
| `fieldkit.companion` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.config` | `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.contact` | `fieldkit.config`, `fieldkit.enrich`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.provenance`, `fieldkit.pursuit`, `fieldkit.sqlite_publication` |
| `fieldkit.driver` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.llm`, `fieldkit.util` |
| `fieldkit.enrich` | `fieldkit.config` |
| `fieldkit.errors` | None observed |
| `fieldkit.gmail` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.google_oauth`, `fieldkit.sqlite_publication`, `fieldkit.sqlite_read` |
| `fieldkit.google_oauth` | `fieldkit.errors` |
| `fieldkit.gtask` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.health` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.ingest` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.google_oauth`, `fieldkit.llm`, `fieldkit.pursuit`, `fieldkit.sqlite_read`, `fieldkit.tasks`, `fieldkit.util` |
| `fieldkit.issue` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.llm` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.meeting` | `fieldkit.config`, `fieldkit.ingest`, `fieldkit.pursuit` |
| `fieldkit.pipeline` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.llm`, `fieldkit.pursuit`, `fieldkit.sf`, `fieldkit.util` |
| `fieldkit.protected_input` | `fieldkit.errors` |
| `fieldkit.provenance` | `fieldkit.pursuit` |
| `fieldkit.publication_policy` | `fieldkit.config` |
| `fieldkit.pursuit` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.quota_period` | None observed |
| `fieldkit.review` | None observed |
| `fieldkit.sf` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.pursuit`, `fieldkit.util` |
| `fieldkit.shadowbot` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.skill` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.llm` |
| `fieldkit.skills` | None observed |
| `fieldkit.sqlite_publication` | `fieldkit._sqlite_publication_storage` |
| `fieldkit.sqlite_read` | `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.tasks` | `fieldkit.util` |
| `fieldkit.util` | `fieldkit.errors` |
| `fieldkit.watch` | `fieldkit.circuit_breaker`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.ingest`, `fieldkit.pursuit`, `fieldkit.util` |
| `fieldkit.web` | `fieldkit.autonomy`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gtask`, `fieldkit.llm` |
| `hooks` | `fieldkit.config`, `fieldkit.publication_policy` |

## Declared architecture boundaries

These are the module paths and allowed dependencies declared in `tach.toml`.
They describe policy, not observed imports or the result of executing Tach.
See the [contribution guide](../CONTRIBUTING.md) for the enforced verification path.

| Declared module | Allowed dependencies |
| --- | --- |
| `fieldkit` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.sf.errors` |
| `fieldkit._sqlite_publication_storage` | `fieldkit.util` |
| `fieldkit.autonomy` | `fieldkit.driver` |
| `fieldkit.brief` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.llm`, `fieldkit.pipeline`, `fieldkit.provenance`, `fieldkit.pursuit`, `fieldkit.util`, `fieldkit.watch` |
| `fieldkit.circuit_breaker` | None declared |
| `fieldkit.commands` | `fieldkit`, `fieldkit.autonomy`, `fieldkit.brief`, `fieldkit.circuit_breaker`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.contact`, `fieldkit.driver`, `fieldkit.enrich`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.google_oauth`, `fieldkit.gtask`, `fieldkit.health`, `fieldkit.ingest`, `fieldkit.issue`, `fieldkit.llm`, `fieldkit.meeting`, `fieldkit.pipeline`, `fieldkit.provenance`, `fieldkit.pursuit`, `fieldkit.sf`, `fieldkit.sf.errors`, `fieldkit.sf.types`, `fieldkit.shadowbot`, `fieldkit.skill`, `fieldkit.sqlite_read`, `fieldkit.tasks`, `fieldkit.util`, `fieldkit.watch`, `fieldkit.web` |
| `fieldkit.companion` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.config` | `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.contact` | `fieldkit.config`, `fieldkit.enrich`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.provenance`, `fieldkit.pursuit`, `fieldkit.sqlite_publication`, `fieldkit.sqlite_read` |
| `fieldkit.driver` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.llm`, `fieldkit.util` |
| `fieldkit.enrich` | `fieldkit.config`, `fieldkit.gmail` |
| `fieldkit.errors` | None declared |
| `fieldkit.gmail` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.google_oauth`, `fieldkit.sqlite_publication`, `fieldkit.sqlite_read` |
| `fieldkit.google_oauth` | `fieldkit.errors` |
| `fieldkit.gtask` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.health` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.ingest` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.google_oauth`, `fieldkit.llm`, `fieldkit.pursuit`, `fieldkit.sqlite_read`, `fieldkit.tasks`, `fieldkit.util` |
| `fieldkit.issue` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.llm` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.meeting` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.ingest`, `fieldkit.pursuit` |
| `fieldkit.pipeline` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.gmail`, `fieldkit.llm`, `fieldkit.pursuit`, `fieldkit.sf`, `fieldkit.util` |
| `fieldkit.provenance` | `fieldkit.pursuit` |
| `fieldkit.pursuit` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.sf` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.pursuit`, `fieldkit.sf._responses`, `fieldkit.sf._transport`, `fieldkit.sf.errors`, `fieldkit.sf.types`, `fieldkit.util` |
| `fieldkit.sf._responses` | `fieldkit.config`, `fieldkit.sf.errors`, `fieldkit.sf.types` |
| `fieldkit.sf._transport` | `fieldkit.config` |
| `fieldkit.sf.errors` | `fieldkit.errors` |
| `fieldkit.sf.types` | None declared |
| `fieldkit.shadowbot` | `fieldkit.config`, `fieldkit.errors` |
| `fieldkit.skill` | `fieldkit.config`, `fieldkit.errors`, `fieldkit.llm` |
| `fieldkit.sqlite_publication` | `fieldkit._sqlite_publication_storage` |
| `fieldkit.sqlite_read` | `fieldkit.errors`, `fieldkit.util` |
| `fieldkit.tasks` | `fieldkit.util` |
| `fieldkit.util` | `fieldkit.errors` |
| `fieldkit.watch` | `fieldkit.circuit_breaker`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.ingest`, `fieldkit.pursuit`, `fieldkit.util` |
| `fieldkit.web` | `fieldkit.autonomy`, `fieldkit.companion`, `fieldkit.config`, `fieldkit.errors`, `fieldkit.gtask`, `fieldkit.llm` |
| `hooks` | `fieldkit`, `fieldkit.config`, `fieldkit.enrich`, `fieldkit.errors` |
