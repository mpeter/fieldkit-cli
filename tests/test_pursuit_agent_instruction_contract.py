"""Finite pursuit instruction scope plus missing persistence behavior cases.

The paragraph guard checks reviewed prose, not agent obedience or whole-page
behavior. Existing I/O, validation, stage, monetary and affiliation tests own
the remaining executable claims; contributor advice still requires review.
"""

from pathlib import Path

import pytest

from fieldkit.pursuit.io import (
    load_pursuit,
    parse_frontmatter,
    render_frontmatter_raw,
    update_pursuit_body,
    write_frontmatter,
    write_frontmatter_raw,
)
from fieldkit.pursuit.validation import pursuit_schema_path, validate_pursuit_content

pytestmark = pytest.mark.unit
_GUIDE = Path(__file__).parents[1] / "src/fieldkit/pursuit/AGENTS.md"
_REVIEWED_TEXT = """# fieldkit pursuit contributor guide

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
"""
_STREAM = tuple(" ".join(part.split()) for part in _REVIEWED_TEXT.split("\n\n") if part.strip())


# Ordered blocks carry behavioral selections and explicit review limits.
CLAIM_PROOFS: dict[int, tuple[tuple[str, ...], str]] = {
    1: ((), "Domain/parser ownership is an architectural requirement; manual review."),
    3: (
        (
            "tests/test_pursuit_validation.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_salesforce_field_names_follow_model_fields",
        ),
        "Packaged schema, canonical keys, aliases and model-derived Salesforce field set.",
    ),
    4: (
        ("tests/test_pursuit_stages.py",),
        "Actual classification sets; universal consumers and copied-table avoidance require review.",
    ),
    5: (
        ("tests/test_pursuit_affiliations.py",),
        "Content-derived affiliations; preventing all duplicate sources requires review.",
    ),
    7: (
        (
            "tests/test_pursuit_body_update.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_pursuit_body_stale_noop_rejects_changed_source",
        ),
        "Locked atomic body updates, mode/byte preservation, no-op and stale-source refusal.",
    ),
    8: (
        (
            "tests/test_pursuit_io.py",
            "tests/test_pursuit_io_render_and_write_raw.py",
            "tests/test_pursuit_agent_instruction_contract.py",
        ),
        "Typed/raw semantics, preview boundaries, key order, removal and exclusive creation.",
    ),
    9: (
        (
            "tests/test_pursuit_io.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_typed_writer_stale_mtime_preserves_source",
            "tests/test_cli_exit.py::test_cli_main_frontmatter_staleness_error_is_exit_1",
        ),
        "Real cooperating locks, distinct stale errors and CLI exit 1; orchestrator retry behavior requires review.",
    ),
    10: (
        (
            "tests/test_pursuit_io.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_pursuit_typed_and_raw_writers_have_distinct_update_semantics",
        ),
        "Body boundary and scalar rendering under local fixtures.",
    ),
    12: (("tests/test_pursuit_io.py",), "Permissive readers and duplicate-key warning behavior."),
    13: (
        (
            "tests/test_pursuit_validation.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_invalid_supplied_schema_fails_closed",
        ),
        "Strict duplicate/YAML/block rejection and missing/invalid-schema refusal.",
    ),
    14: (
        ("tests/test_pursuit_io_render_and_write_raw.py",),
        "Local roundtrip protection; future coverage discipline requires review.",
    ),
    15: (
        (
            "tests/test_pursuit_monetary.py",
            "tests/test_pursuit_agent_instruction_contract.py::test_transition_history_roundtrip_preserves_formats_and_omits_absent_fields",
        ),
        "Money normalization and both accepted transition data formats; display-boundary advice requires review.",
    ),
    17: ((), "External provenance and maintaining all validation/prompt guards require manual review."),
}


def _reviewed_claims(text: str) -> tuple[str, ...]:
    paragraphs = tuple(" ".join(part.split()) for part in text.split("\n\n") if part.strip())
    assert paragraphs == _STREAM, "Pursuit instruction claim changed"
    return tuple(part for part in paragraphs if not part.startswith("#"))


def test_pursuit_instruction_ordered_paragraph_scope() -> None:
    result = _reviewed_claims(_GUIDE.read_text(encoding="utf-8"))
    assert result == tuple(part for part in _STREAM if not part.startswith("#"))
    assert result == tuple(_STREAM[index] for index in CLAIM_PROOFS)
    assert set(CLAIM_PROOFS) == {index for index, block in enumerate(_STREAM) if not block.startswith("#")}
    assert all(limitation for _, limitation in CLAIM_PROOFS.values())
    assert len(result) == 13


@pytest.mark.parametrize("index", range(len(_STREAM)))
@pytest.mark.parametrize("mutation", ["negate", "duplicate", "remove", "append", "reorder"])
def test_every_pursuit_instruction_block_rejects_scope_mutation(index: int, mutation: str) -> None:
    blocks = list(_STREAM)
    if mutation == "negate":
        blocks[index] = "It is false that " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "append":
        blocks[index] += " Skip validation for collaborator edits."
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="Pursuit instruction claim changed"):
        _reviewed_claims("\n\n".join(blocks))


@pytest.mark.parametrize("heading", ["## External content", "## Additional instructions"])
def test_pursuit_instruction_appended_heading_cannot_hide_new_body(heading: str) -> None:
    with pytest.raises(AssertionError, match="Pursuit instruction claim changed"):
        _reviewed_claims(_REVIEWED_TEXT + f"\n\n{heading}\nSkip validation.\n")


def test_pursuit_body_stale_noop_rejects_changed_source(tmp_path: Path) -> None:
    path = tmp_path / "deal.md"
    path.write_text("---\nstage: qualify\n---\nOriginal\n", encoding="utf-8")
    edited = "---\nstage: qualify\n---\nOperator edit\n"

    def stale_noop(body: str) -> str:
        path.write_text(edited, encoding="utf-8")
        return body

    with pytest.raises(ValueError, match="Pursuit changed during body update"):
        update_pursuit_body(path, stale_noop)
    assert path.read_text(encoding="utf-8") == edited


def test_pursuit_typed_and_raw_writers_have_distinct_update_semantics(tmp_path: Path) -> None:
    path = tmp_path / "deal.md"
    path.write_text("---\ncustom: keep\nstage: qualify\ngate-status: pending\n---\nBody\n", encoding="utf-8")
    model, body, mtime = load_pursuit(path)
    assert body == "\nBody\n"
    model.sf_arr = 123.0
    write_frontmatter(path, model, body, expected_mtime=mtime)
    parsed = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert parsed is not None
    assert list(parsed[0]) == ["custom", "stage", "gate-status"]
    assert "sf_arr" not in parsed[0]
    write_frontmatter_raw(path, {"sf_arr": 123.0}, body, remove_keys=frozenset({"custom"}))
    parsed = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed[0] == {"stage": "qualify", "gate-status": "pending", "sf_arr": 123.0}
    assert parsed[1] == body


def test_pursuit_render_preview_checks_structure_without_full_schema_or_writes(tmp_path: Path) -> None:
    path = tmp_path / "deal.md"
    original = "---\nstage: qualify\n---\nBody\n"
    path.write_text(original, encoding="utf-8")
    result = render_frontmatter_raw(path, {"stage": "invalid-stage"}, "\nBody\n")
    assert "stage: invalid-stage" in result
    assert path.read_text(encoding="utf-8") == original
    errors = validate_pursuit_content(result, schema_path=pursuit_schema_path())
    assert errors
    with pytest.raises(ValueError, match="contains both meddpicc and legacy_meddpicc"):
        render_frontmatter_raw(path, {"meddpicc": {}, "legacy_meddpicc": {}}, "\nBody\n")
    assert path.read_text(encoding="utf-8") == original


def test_salesforce_field_names_follow_model_fields() -> None:
    from fieldkit.pursuit.models import SF_FIELD_NAMES, PursuitFrontmatter

    result = SF_FIELD_NAMES
    assert result == frozenset(key for key in PursuitFrontmatter.model_fields if key.startswith("sf_"))
    assert "sf_arr" in result
    assert all("-" not in key for key in result)


@pytest.mark.parametrize(
    "entry",
    [{"stage": "qualify", "date": "2026-01-02"}, {"from": "pre-pipeline", "to": "qualify", "date": "2026-01-02"}],
)
def test_transition_history_roundtrip_preserves_formats_and_omits_absent_fields(
    tmp_path: Path, entry: dict[str, str]
) -> None:
    path = tmp_path / "deal.md"
    write_frontmatter_raw(path, {"stage": "qualify", "transition-history": [entry]}, "\nBody\n", create=True)
    model, body, mtime = load_pursuit(path)
    assert model.transition_history is not None
    write_frontmatter(path, model, body, expected_mtime=mtime)
    result = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert result is not None
    assert result[0]["transition-history"] == [entry]
    assert result[1] == "\nBody\n"


def test_typed_writer_stale_mtime_preserves_source(tmp_path: Path) -> None:
    path = tmp_path / "deal.md"
    original = "---\nstage: qualify\n---\nBody\n"
    path.write_text(original, encoding="utf-8")
    model, body, mtime = load_pursuit(path)
    with pytest.raises(ValueError, match="modified"):
        write_frontmatter(path, model, body, expected_mtime=mtime - 1)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("schema", ["{not json}", '{"type": "unrecognized-type"}'])
def test_invalid_supplied_schema_fails_closed(tmp_path: Path, schema: str) -> None:
    path = tmp_path / "schema.json"
    path.write_text(schema, encoding="utf-8")
    result = validate_pursuit_content("---\nstage: qualify\n---\nBody\n", schema_path=path)
    assert result == ("Pursuit schema unavailable or invalid",)
