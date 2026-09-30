# fieldkit pursuit contributor guide

Pursuit models and I/O preserve the contract between local Markdown frontmatter
and domain consumers. Read and write pursuits through `fieldkit.pursuit.io`;
do not build a second parser or writer inside a command.

## Models and canonical values

The packaged pursuit-frontmatter schema defines the document contract.
`PursuitFrontmatter` provides typed access, and `SF_FIELD_NAMES` is derived from
its model fields. Salesforce keys use underscores. Preserve the deliberate
hyphenated aliases for lifecycle fields when rendering YAML.

Use `Stage` and `MEDDPICCElement` from `fieldkit.pursuit.enums` and the
classification sets in `fieldkit.pursuit.stages`. Do not copy stage strings or
ordering tables into consumers. `TERMINAL_STAGES` includes informal terminal
aliases in addition to `CLOSED_STAGES`; use the former for stall detection and
the latter for closed-deal filtering.

`fieldkit.contact.resolver.scan_pursuit_affiliations()` owns stakeholder
affiliations, which are derived from pursuit content rather than persisted
as a second set of role fields. Adding a separate champion or economic-buyer
field would create another source that can drift from the document.

## Choose the correct writer

For body-only edits, use `update_pursuit_body()` with a pure transform. It uses
the canonical bounded pursuit lock, validates frontmatter while preserving its
bytes, and compares the source identity and content before writing. A stale
source is rejected even for a no-op. An unchanged body returns `False`; a
changed body is replaced atomically with the existing file mode and returns
`True`. This avoids reserializing frontmatter for a Markdown-only update.

`write_frontmatter()` renders a typed model while preserving existing key order;
it does not add Salesforce fields absent from the original file.
`write_frontmatter_raw()` supports raw field updates, new keys, explicit key
removal, and creation. Do not assume these two interfaces have identical update
semantics. `render_frontmatter_raw()` applies write-path structural checks and
renders a proposed replacement without writing it. It does not validate the full
packaged schema or typed model contract.

Both writers serialize cooperating writes with the pursuit's runtime lock and
replace the file atomically. Pass the mtime returned by `load_pursuit()` as
`expected_mtime` for load-modify-write cycles. Currently the typed writer raises
`ValueError` on a stale mtime; the raw writer raises
`fieldkit.errors.FrontmatterStalenessError`. Do not document or catch them as
though they were the same exception. The CLI maps the latter to exit 1.

The body passed to the writers starts with the newline after the closing YAML
delimiter. Preserve it instead of reconstructing the Markdown body.
Use the existing scalar-rendering helpers for untrusted values, including values
containing `---`; string interpolation can corrupt document boundaries.

## Parsing and round trips

`parse_frontmatter()` and `parse_frontmatter_fallback()` live in
`fieldkit.pursuit.io`. The fallback delegates to the ordinary parser before
handling empty blocks. These are permissive readers: duplicate mapping keys
warn and the last value wins.

`fieldkit.pursuit.validation.parse_pursuit_content()` is the strict document
parser. It rejects duplicate keys, invalid YAML, and adjacent frontmatter
blocks. `validate_pursuit_content()` also checks the supplied schema and fails
closed when that schema is missing or invalid. Choose according to the input
contract; a successful permissive read is not proof of strict validity.

Test unknown keys, aliases, key order, body preservation, stale writes, and
duplicate YAML keys when changing persistence. Model loading and writing are
different operations: do not assume typed model serialization alone preserves
every source field.

Monetary parsing belongs in the existing normalization helpers. Keep display
formatting at the rendering boundary rather than persisting formatted currency.
Transition history supports both legacy stage entries and from/to entries;
preserve their aliases and omit absent values when rendering. These accepted
data formats are distinct from removed CLI or configuration aliases.

## External content

Treat synchronized workbook and integration content as external input even when
the usual editor is the workspace owner. Editing access and provenance can
change. Preserve validation and prompt guards rather than assuming collaborator
edits cannot occur.
