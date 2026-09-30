"""Fail-closed pursuit frontmatter validation contracts."""

import json
import traceback
from datetime import date
from pathlib import Path

import pytest

from fieldkit.errors import FieldkitError
from fieldkit.pursuit.validation import (
    inspect_pursuit_quality,
    parse_pursuit_content,
    pursuit_schema_path,
    validate_pursuit_content,
)

pytestmark = pytest.mark.unit


def test_cyclic_alias_is_a_fixed_validation_error() -> None:
    result = validate_pursuit_content(
        "---\nstage: discover\ngate-status: pending\nloop: &cycle [*cycle]\n---\n",
        schema_path=pursuit_schema_path(),
    )

    assert result == ("Invalid pursuit frontmatter",)


def test_cyclic_alias_cannot_enter_quality_traversal() -> None:
    with pytest.raises(FieldkitError, match=r"^Invalid pursuit frontmatter$") as caught:
        inspect_pursuit_quality("---\nloop: &cycle [*cycle]\n---\n")

    assert "RecursionError" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("schema", "frontmatter", "keyword"),
    [
        ({"properties": {"stage": {"enum": ["discover"]}}}, "stage: private_value_marker", "enum"),
        ({"properties": {"sf_account": {"pattern": "^acme$"}}}, "sf_account: private_value_marker", "pattern"),
        ({"additionalProperties": False}, "private_key_marker: retained", "additionalProperties"),
        ({"properties": {"sf_account": {"type": "string"}}}, "sf_account: {private_key_marker: retained}", "type"),
    ],
)
def test_schema_diagnostics_do_not_reflect_frontmatter(
    tmp_path: Path, schema: dict[str, object], frontmatter: str, keyword: str
) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    errors = validate_pursuit_content(f"---\n{frontmatter}\n---\n", schema_path=schema_path)

    assert errors == (f"Pursuit frontmatter schema violation ({keyword})",)
    assert "private_value_marker" not in " ".join(errors)
    assert "private_key_marker" not in " ".join(errors)


@pytest.mark.parametrize("key", ["sf-private-marker", '"sf-private-marker"', "'sf-private-marker'", "sf-Owner"])
def test_canonical_validation_rejects_hyphenated_salesforce_keys_without_reflection(key: str) -> None:
    content = f"---\nstage: discover\ngate-status: pending\n{key}: acme-corp\n---\n# Notes\n"

    errors = validate_pursuit_content(content, schema_path=pursuit_schema_path())

    assert errors == ("Hyphenated Salesforce frontmatter keys are invalid; use underscores",)
    assert key not in " ".join(errors)
    assert "acme-corp" not in " ".join(errors)


def test_canonical_validation_preserves_lifecycle_aliases_and_other_fields() -> None:
    content = (
        "---\nstage: discover\ngate-status: pending\nlast-transition: 2026-09-29\n"
        "last-updated: 2026-09-29\nsf_name: Expansion\ncustom-field: retained\n"
        "details:\n  sf-custom-field: nested body data\n---\n# Notes\n"
    )

    errors = validate_pursuit_content(content, schema_path=pursuit_schema_path())

    assert errors == ()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("---\nstage: discover\nstage: qualify\n---\nBody\n", "Duplicate pursuit frontmatter mapping keys"),
        ("---\nstage: discover\n---\n---\naccount: acme\n---\nBody\n", "multiple frontmatter blocks"),
    ],
)
def test_strict_parser_rejects_ambiguous_frontmatter(content: str, message: str) -> None:
    with pytest.raises(FieldkitError, match=message):
        parse_pursuit_content(content)


def test_missing_schema_is_a_validation_error(tmp_path: Path) -> None:
    errors = validate_pursuit_content(
        "---\nstage: discover\n---\n",
        schema_path=tmp_path / "missing.json",
    )

    assert errors == ("Pursuit schema unavailable or invalid",)


def test_quality_inspection_rejects_invalid_yaml_instead_of_passing() -> None:
    with pytest.raises(FieldkitError, match="Invalid pursuit frontmatter"):
        inspect_pursuit_quality("---\nstage: [unterminated\n---\n")


def test_quality_inspection_counts_nested_backstory_references() -> None:
    result = inspect_pursuit_quality("---\nstage: discover\nsignals:\n  - source: '[Backstory] cached claim'\n---\n")

    assert result.has_frontmatter is True
    assert result.backstory_paths == ("signals[0].source",)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "details:\n  private_marker: first\n  private_marker: second\n",
        "signals:\n  - private_marker: first\n    private_marker: second\n",
        "defaults: &defaults\n  private_marker: first\ndetails:\n  <<: *defaults\n  private_marker: second\n",
    ],
    ids=["nested-mapping", "list-mapping", "merge-collision"],
)
def test_strict_parser_rejects_nested_duplicate_keys(frontmatter: str) -> None:
    with pytest.raises(FieldkitError, match=r"^Duplicate pursuit frontmatter mapping keys$") as caught:
        parse_pursuit_content(f"---\n{frontmatter}---\nBody\n")

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "private_marker" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "frontmatter",
    [
        "? [private_marker, other]\n: value\n",
        "details:\n  ? [private_marker, other]\n  : value\n",
        "details:\n  private_marker: [unterminated\n",
        "42: private_marker\n",
        "null: private_marker\n",
    ],
    ids=["non-scalar-root", "non-scalar-nested", "malformed-nested", "numeric-root", "null-root"],
)
def test_strict_parser_rejects_invalid_keys_and_nested_yaml(frontmatter: str) -> None:
    with pytest.raises(FieldkitError, match=r"^Invalid pursuit frontmatter$") as caught:
        parse_pursuit_content(f"---\n{frontmatter}---\nBody\n")

    assert caught.value.__cause__ is None
    assert "private_marker" not in "".join(traceback.format_exception(caught.value))


def test_strict_parser_preserves_unambiguous_nested_aliases_dates_and_body() -> None:
    body = "\n# Narrative\r\n\n---\nBody marker stays intact.\n"
    parsed = parse_pursuit_content(
        "---\nstage: discover\nclose_date: 2026-09-28\ndefaults: &defaults\n  source: verified\n"
        "details:\n  <<: *defaults\n  owner: acme\nsignals:\n  - *defaults\n---" + body
    )

    assert parsed is not None
    assert parsed.frontmatter["details"] == {"source": "verified", "owner": "acme"}
    assert parsed.frontmatter["signals"] == [{"source": "verified"}]
    assert parsed.frontmatter["close_date"] == date(2026, 9, 28)
    assert parsed.body == body
