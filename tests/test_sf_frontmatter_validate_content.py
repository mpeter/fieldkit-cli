"""Coverage for ``_validate_frontmatter_content`` error paths.

This is a tests-only addition targeting a single function in
``fieldkit.commands.sf.frontmatter``. It intentionally does NOT modify
``tests/test_sf_frontmatter.py`` — that file only ever patches
``_validate_frontmatter_content`` wholesale as a test double for other
tests and never exercises its own internals.
"""

import json
from pathlib import Path

import pytest

from fieldkit.commands.sf import frontmatter

pytestmark = pytest.mark.unit


# ── local helpers ────────────────────────────────────────────────────────────


def _make_schema_file(tmp_path: Path, schema: dict[str, object]) -> Path:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    return schema_path


def _minimal_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "sf_account": {"type": "string"},
        },
        "required": ["sf_account"],
    }


def _make_content(frontmatter_body: str) -> str:
    return f"---\n{frontmatter_body}\n---\n\nBody text.\n"


# ── behavior 1: schema file missing (OSError) ───────────────────────────────


def test_schema_file_missing_warns_and_returns_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing_schema_path = tmp_path / "nope.json"
    content = _make_content("sf_account: Acme Corp")

    result = frontmatter._validate_frontmatter_content(content, missing_schema_path)

    assert result == []
    captured = capsys.readouterr()
    assert f"WARNING: Could not load schema at {missing_schema_path}:" in captured.err


# ── behavior 2: schema file has invalid JSON ────────────────────────────────


def test_schema_file_invalid_json_warns_and_returns_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad_schema_path = tmp_path / "schema.json"
    bad_schema_path.write_text("{not valid json", encoding="utf-8")
    content = _make_content("sf_account: Acme Corp")

    result = frontmatter._validate_frontmatter_content(content, bad_schema_path)

    assert result == []
    captured = capsys.readouterr()
    assert f"WARNING: Could not load schema at {bad_schema_path}:" in captured.err


# ── behavior 3: fewer than 2 dash delimiters ────────────────────────────────


def test_no_frontmatter_delimiters_returns_empty(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content_no_dashes = "Just body text, no frontmatter delimiters at all.\n"

    assert frontmatter._validate_frontmatter_content(content_no_dashes, schema_path) == []


def test_single_frontmatter_delimiter_returns_empty(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content_one_dash = "---\nsf_account: Acme Corp\n"

    assert frontmatter._validate_frontmatter_content(content_one_dash, schema_path) == []


# ── behavior 4: invalid YAML in frontmatter block ───────────────────────────


def test_invalid_yaml_frontmatter_returns_parse_error(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    # Unbalanced flow-mapping brackets is invalid YAML.
    invalid_yaml_body = "sf_account: [unclosed"
    content = _make_content(invalid_yaml_body)

    result = frontmatter._validate_frontmatter_content(content, schema_path)

    assert len(result) == 1
    assert result[0].startswith("YAML parse error: ")


# ── behavior 5: happy path — schema violations formatted per-error ─────────


def test_schema_violations_formatted_as_json_path_and_message(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    # Missing the required "sf_account" property triggers a validation error.
    content = _make_content("sf_other_field: acme-corp.com")

    result = frontmatter._validate_frontmatter_content(content, schema_path)

    assert len(result) == 1
    assert result[0].startswith("[") and "] " in result[0]
    assert "sf_account" in result[0]


def test_valid_frontmatter_returns_no_errors(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content = _make_content("sf_account: Acme Corp")

    assert frontmatter._validate_frontmatter_content(content, schema_path) == []
