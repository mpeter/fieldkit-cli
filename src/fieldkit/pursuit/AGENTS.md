# AGENTS.md — Pursuit Domain (`pursuit/`)

Pydantic models, I/O, and helpers for pursuit `.md` files. Manages the frontmatter contract between the local vault and Salesforce.

## Affiliations: Derive, Never Persist (D018)

**CONSTRAINT (hard, non-reglobalpayble):** Pursuit stakeholder affiliations (champion, economic buyer, etc.) are **always derived from pursuit file content at read time**. They are **never persisted** as separate structured fields in any database or file.

Rationale: Persisted affiliations drifted from source truth and produced frequent false positives. Disk-based derivation is always current — if a stakeholder is removed from a pursuit file, the affiliation disappears immediately. This is a hard architectural constraint based on observed data quality issues.

Do not add a `champion` field, `economic_buyer` field, or any affiliation field to `PursuitFrontmatter`. Do not write affiliation data to any database.

## Frontmatter Key Naming (R05)

**CONSTRAINT:** All `sf_*` frontmatter keys use underscores, never hyphens. The SF pipeline only reads underscore-keyed fields; hyphenated variants are silently ignored.

Active `sf_*` fields (source of truth: `src/fieldkit/_data/pursuit-frontmatter.schema.json`):
- `sf_opportunity_id`, `sf_stage`, `sf_close_date`
- `sf_arr`, `sf_acv`, `sf_consulting_acv`, `sf_training_acv`
- `sf_owner`, `sf_next_steps`, `sf_last_pulled`
- `sf_deal_splits`, `sf_probability`

Non-SF frontmatter fields that use hyphens (these are intentional aliases in the Pydantic model): `gate-status`, `last-transition`, `last-updated`, `transition-history`.

## Schema Source of Truth

`src/fieldkit/_data/pursuit-frontmatter.schema.json` is the single canonical schema definition. `SF_FIELD_NAMES` in `models.py` is derived from `PursuitFrontmatter.model_fields` — adding a new `sf_` field to the model automatically updates the set used by `io.py`'s round-trip preservation.

`PursuitFrontmatter` uses `ConfigDict(extra='ignore')` — unknown keys are silently discarded on load. This is intentional for forward compatibility. Do not tighten to `extra='forbid'` until the field set has stabilized.

## Round-Trip Invariant

`load_pursuit()` → `write_frontmatter()` must leave the file semantically identical. `write_frontmatter()` re-reads the current file to preserve original YAML key ordering — it only writes keys that were present in the original. It does NOT add new `sf_*` fields that weren't already in the file.

**GOTCHA:** `write_frontmatter()` accepts an `expected_mtime` parameter (the `mtime` float from `load_pursuit()`). If the file was modified between load and write, it raises `ValueError`. Always pass `expected_mtime` when doing a load-modify-write cycle to detect concurrent modifications.

## Canonical Write Path: `write_frontmatter_raw()` (Spec 021b)

All frontmatter writes MUST go through `write_frontmatter_raw()` in `pursuit/io.py`. Private `_write_frontmatter()` helpers in individual command modules have been removed — do not re-introduce them.

`write_frontmatter_raw(path, fm, body, *, create=False, expected_mtime=None)` — atomic (temp + `Path.replace()`), key-order-preserving. Body must start with `'\n'` (the newline immediately after the closing `---` delimiter). `create=True` skips the on-disk read and uses `fm` iteration order; `create=False` (default) re-reads the file to preserve existing key order.

`FrontmatterStalenessError(FieldkitError)` is raised on `expected_mtime` mismatch (re-parented from `ValueError` to `FieldkitError` in #1169). `cli_main()` catches it via `isinstance(exc, FrontmatterStalenessError)` and maps it to `EXIT_PARTIAL` (1). The class lives in `fieldkit.errors` (tach-safe, zero deps) — import from there, not from `fieldkit.pursuit` or `fieldkit.pursuit.io`.

## YAML Scalar Quoting: Values Containing `---`

**GOTCHA:** `_yaml_scalar()` in `io.py` must quote values that contain `---` (the YAML document separator). Before Spec 021b/historic regression, such values caused YAML document separator injection — the YAML parser treated `---` in a value as a new document start, silently truncating everything after it.

## Stage Constants: Single Source of Truth

Import `CLOSED_STAGES`, `TERMINAL_STAGES`, `PIPELINE_STAGES` from `fieldkit.pursuit.stages` (re-exported via `pursuit/__init__.py`). Do NOT define local `CLOSED_STAGES = frozenset({...})` inline in command modules — that pattern was the root cause of drift caught in Spec 021.

`TERMINAL_STAGES` ⊃ `CLOSED_STAGES` — use `TERMINAL_STAGES` for stall detection ("is this pursuit done?"), `CLOSED_STAGES` for pipeline filtering and archival.

## `parse_frontmatter()` vs `parse_frontmatter_fallback()`

`parse_frontmatter()` (from `fieldkit.pursuit`) is the canonical parser for
ordinary frontmatter. `parse_frontmatter_fallback()` in `fieldkit.pursuit.io`
delegates to it first, then handles edge cases such as an empty block
(`---\n---`) that the ordinary parser skips.

Consumers that need empty-frontmatter support import `parse_frontmatter_fallback`
from `fieldkit.pursuit.io`; the audit command does not own a parser wrapper.

## Workbook Sync Model (D050/D051)

Pursuit workbooks (Google Docs with N tabs accumulating pursuit context) are treated as trusted signal. Sync-back from workbook to vault is automatic — the user is the primary editor; external collaborator edits are not a real scenario. The workbook doc ID is stored as `source_id` on the vault pursuit file.

## Stage and MEDDPICC Enums (implementation note)

`Stage(StrEnum)` and `MEDDPICCElement(StrEnum)` live in `src/fieldkit/pursuit/enums.py`. Use them instead of raw strings — the JSON schema, stage ordering, and gate criteria are all derived from these enums at import time. `Stage.PRE_PIPELINE.value` is used as the Click default to preserve help text (enum member, not raw string).

`STAGE_ORDER` (ordered list) lives in `pursuit/stages.py`; `GATE_CRITERIA` (per-stage MEDDPICC requirements) is canonical in `stages.py` and imported by `pipeline/collect.py` — do not redefine locally.

## PursuitStaleError (implementation note)

`PursuitStaleError(FieldkitError)` in `src/fieldkit/errors.py` replaces the old `raise SystemExit(1)` in `pursuit/stale.py`. Domain code must never call `sys.exit()` or `raise SystemExit()` — raise `PursuitStaleError` instead and let `cli_main()` map it to EXIT_PARTIAL (1) via type-name check.

## Advance Workflow

`fieldkit pursuit advance` updates `sf_stage` in frontmatter and optionally pushes to Salesforce. It does **not** auto-advance based on MEDDPICC completeness — stage advancement is always a deliberate human action.

## `pursuit audit` Positional Account Deprecation (implementation change)

`pursuit audit` is the only subcommand that ever accepted a bare positional `ACCOUNT` argument. All other pursuit subcommands (`health`, `projects`, `forecast`, `advance`, `archive`) use `--account`/`-a` only. The positional form in `audit_cmd.py` is now deprecated — it still works but emits a warning to stderr. The Click param is `account_pos`; resolution logic is in `audit_cmd.py:cli()` around line 245. Do not add positional account args to any new subcommand.

## Monetary Field Normalization

`sf_arr`, `sf_acv`, `sf_consulting_acv`, `sf_training_acv` are normalized to `float` on load via `_parse_monetary()`. Accepts `"$500,000"`, `500000.0`, `500000`, `None`, or `""`. Non-numeric strings (legacy free text) are returned as-is. Display formatting is applied at render time only — never store formatted strings back to frontmatter.

## Transition History Schema Note

`TransitionEntry` has a `stage` field (Schema A legacy) alongside `from_`/`to` (Schema B). Both coexist in real pursuit files. `_render_transition_history()` uses `model_dump(by_alias=True, exclude_none=True)` to avoid writing spurious null fields.
