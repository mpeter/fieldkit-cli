## Context

`NON_PURSUIT_FILES` in `fieldkit.pursuit.io` already backs `scan_report_inputs()`,
whose `ReportAssessment` (from #97) counts `scanned`, `included`, `excluded`,
and `failures`. The forecast and health footers print the excluded count but
not which files were excluded. Nine other sites carry their own rules,
summarized in the proposal; three of them disagree with the constant.

## Goals / Non-Goals

### Goals
- One definition and one exact-name predicate.
- Refuse reserved slugs before any write in create and rename.
- Make every reserved-file skip visible by path in report diagnostics.

### Non-Goals
- Changing what is reserved, or adding new reserved names.
- Migrating or renaming files in existing workspaces.
- Validating other slug properties in `rename --to` (path separators, empty
  slugs). That is a separate defect if it exists, and is noted under Risks.

## Decisions

1. **Predicate in the domain.** Add `is_reserved_pursuit_path(path: Path) -> bool`
   and `RESERVED_PURSUIT_SLUGS` beside `NON_PURSUIT_FILES` in
   `fieldkit/pursuit/io.py`, which is already the authoritative pursuit I/O
   module. Command modules import it; no command keeps a literal. This follows
   the repository rule that constants have one home.
2. **Exact file name, not substring.** `iterate_pursuits()` switches from
   substring tests to the predicate. Its glob is already limited to
   `accounts/*/pursuits/*.md`, so the `.template/` directory case it was
   guarding against can't occur there. `sf frontmatter --validate` keeps its
   separate `.template/` directory rule because it validates arbitrary paths;
   it adopts the predicate for the file-name half.
3. **Refuse in the command adapter, decide in the domain.** `create_cmd` and
   `rename_cmd` call the predicate on the target path after slugifying and
   before any filesystem access, and exit through the existing `EXIT_DATA`
   path with a JSON error when `--json` is set.
4. **Disclosure through `ReportAssessment`.** Add a `reserved` list of
   relative paths to the assessment and render it in the existing diagnostics
   channel (stderr for text, the diagnostics field for JSON). `excluded`
   arithmetic is unchanged, so the #97 contract holds.

## Risks / Trade-offs

- **Output changes for brief, pipeline, and quota.** Workspaces with a pursuits-level
  `template.md` will stop seeing it as a pursuit, and pursuits whose paths
  contain `gmail-intel` will reappear. Both are corrections; the changelog
  fragment calls them out.
- **`rename --to` slug hygiene.** The `--to` value is used as a file name
  without slugification. This change validates only reservation; if a path
  separator check is missing, file it as its own issue rather than widening
  this change.
