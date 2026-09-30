"""Fail-closed pursuit schema validation and non-writing error paths."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit import __main__ as dispatcher
from fieldkit.commands.sf import frontmatter
from fieldkit.pursuit.validation import validate_pursuit_content
from fieldkit.sf import frontmatter as domain_frontmatter

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "frontmatter",
    [
        "loop: &cycle [*cycle]",
        "\n".join(
            ["node0: &node0 [fictional]"] + [f"node{i}: &node{i} [*node{i - 1}, *node{i - 1}]" for i in range(1, 13)]
        ),
    ],
    ids=["cycle", "expanded-alias-limit"],
)
def test_validation_command_rejects_alias_graph_without_traceback_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], frontmatter: str
) -> None:
    path = tmp_path / "pursuit.md"
    content = f"---\nstage: discover\ngate-status: pending\n{frontmatter}\n---\n"
    path.write_text(content, encoding="utf-8")
    before = path.stat().st_mtime_ns
    monkeypatch.setattr(dispatcher, "load_dotenv_safe", lambda: None)

    result = dispatcher.main(["sf", "frontmatter", "--validate", "--file", str(path), "--json"])
    captured = capsys.readouterr()

    assert result == 1
    assert json.loads(captured.out)["errors"] == ["Invalid pursuit frontmatter"]
    assert "Traceback" not in captured.err
    assert "RecursionError" not in captured.err
    assert "cycle" not in captured.out + captured.err
    assert path.read_text(encoding="utf-8") == content
    assert path.stat().st_mtime_ns == before


def test_validation_command_does_not_reflect_invalid_frontmatter_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pursuit.md"
    content = "---\nstage: private_value_marker\ngate-status: pending\n---\n# Notes\n"
    path.write_text(content, encoding="utf-8")
    before = path.stat().st_mtime_ns
    monkeypatch.setattr(dispatcher, "load_dotenv_safe", lambda: None)

    result = dispatcher.main(["sf", "frontmatter", "--validate", "--file", str(path), "--json"])

    assert result == 1
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["status"] == "invalid"
    assert document["errors"] == ["Pursuit frontmatter schema violation (enum)"]
    assert "private_value_marker" not in captured.out + captured.err
    assert path.read_text(encoding="utf-8") == content
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("field", "status", "exit_code"),
    [("sf-private-marker", "invalid", 1), ('"sf-private-marker"', "invalid", 1), ("sf_name", "valid", 0)],
)
def test_documented_validation_command_preserves_file_and_salesforce_key_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    status: str,
    exit_code: int,
) -> None:
    path = tmp_path / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True)
    content = f"---\nstage: discover\ngate-status: pending\n{field}: Expansion\ncustom-field: retained\n---\n# Notes\n"
    path.write_text(content, encoding="utf-8")
    before = path.stat().st_mtime_ns
    monkeypatch.setattr(dispatcher, "load_dotenv_safe", lambda: None)

    result = dispatcher.main(["sf", "frontmatter", "--validate", "--file", str(path), "--json"])

    assert result == exit_code
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["status"] == status
    assert document["errors"] == (
        ["Hyphenated Salesforce frontmatter keys are invalid; use underscores"] if exit_code else []
    )
    assert "sf-private-marker" not in captured.out + captured.err
    assert path.read_text(encoding="utf-8") == content
    assert path.stat().st_mtime_ns == before


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


def test_schema_file_missing_returns_validation_error(tmp_path: Path) -> None:
    missing_schema_path = tmp_path / "nope.json"
    content = _make_content("sf_account: Acme Corp")

    result = validate_pursuit_content(content, schema_path=missing_schema_path)

    assert result == ("Pursuit schema unavailable or invalid",)


# ── behavior 2: schema file has invalid JSON ────────────────────────────────


def test_schema_file_invalid_json_returns_validation_error(tmp_path: Path) -> None:
    bad_schema_path = tmp_path / "schema.json"
    bad_schema_path.write_text("{not valid json", encoding="utf-8")
    content = _make_content("sf_account: Acme Corp")

    result = validate_pursuit_content(content, schema_path=bad_schema_path)

    assert result == ("Pursuit schema unavailable or invalid",)


def test_schema_file_invalid_contract_returns_validation_error(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, {"type": "not-a-json-schema-type"})

    result = validate_pursuit_content(_make_content("sf_account: Acme Corp"), schema_path=schema_path)

    assert result == ("Pursuit schema unavailable or invalid",)


# ── behavior 3: fewer than 2 dash delimiters ────────────────────────────────


def test_no_frontmatter_delimiters_returns_validation_error(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content_no_dashes = "Just body text, no frontmatter delimiters at all.\n"

    result = validate_pursuit_content(content_no_dashes, schema_path=schema_path)
    assert result == ("Missing or incomplete pursuit frontmatter",)


def test_single_frontmatter_delimiter_returns_validation_error(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content_one_dash = "---\nsf_account: Acme Corp\n"

    result = validate_pursuit_content(content_one_dash, schema_path=schema_path)
    assert result == ("Missing or incomplete pursuit frontmatter",)


# ── behavior 4: invalid YAML in frontmatter block ───────────────────────────


def test_invalid_yaml_frontmatter_returns_parse_error(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    # Unbalanced flow-mapping brackets is invalid YAML.
    invalid_yaml_body = "sf_account: [unclosed"
    content = _make_content(invalid_yaml_body)

    result = validate_pursuit_content(content, schema_path=schema_path)

    assert result == ("Invalid pursuit frontmatter",)


# ── behavior 5: happy path — schema violations formatted per-error ─────────


def test_schema_violations_identify_the_check_without_reflecting_contents(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    # Missing the required "sf_account" property triggers a validation error.
    content = _make_content("sf_other_field: acme-corp.example.com")

    result = validate_pursuit_content(content, schema_path=schema_path)

    assert result == ("Pursuit frontmatter schema violation (required)",)
    assert "acme-corp.example.com" not in " ".join(result)


def test_valid_frontmatter_returns_no_errors(tmp_path: Path) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    content = _make_content("sf_account: Acme Corp")

    assert validate_pursuit_content(content, schema_path=schema_path) == ()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("schema_mode", ["missing", "corrupt", "invalid"])
def test_unusable_schema_refuses_opportunity_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, schema_mode: str
) -> None:
    path = tmp_path / "accounts" / "acme-corp" / "pursuits" / "pursuit.md"
    path.parent.mkdir(parents=True)
    original = "---\nstage: discover\ngate-status: pending\n---\n\n# Body\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat().st_mtime_ns
    schema_path = tmp_path / "schema.json"
    if schema_mode == "corrupt":
        schema_path.write_text("{invalid", encoding="utf-8")
    elif schema_mode == "invalid":
        schema_path.write_text(json.dumps({"type": "invalid"}), encoding="utf-8")
    monkeypatch.setattr(frontmatter, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(domain_frontmatter, "pursuit_schema_path", lambda: schema_path)
    payload = json.dumps({"status": "ok", "opportunity_id": "006000000000AAA", "stage": "Propose"})
    args = [str(path), payload]
    if dry_run:
        args.insert(0, "--dry-run")

    result = CliRunner().invoke(frontmatter.cli, args)

    assert result.exit_code != 0
    assert "Pursuit schema unavailable or invalid" in str(result.exception)
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before


def test_missing_schema_never_reports_valid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "pursuit.md"
    path.write_text(_make_content("sf_account: Acme Corp"), encoding="utf-8")
    monkeypatch.setattr(frontmatter, "pursuit_schema_path", lambda: tmp_path / "missing.json")

    result = CliRunner().invoke(frontmatter.cli, ["--validate", "--json", "--file", str(path)])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["status"] == "invalid"


@pytest.mark.parametrize(
    "content",
    [
        "Body introduction\n---\nsf_account: Acme Corp\n---\nBody\n",
        "---\nsf_account: First\nsf_account: Second\n---\nBody\n",
    ],
    ids=["body-horizontal-rules", "duplicate-mapping-key"],
)
def test_ambiguous_frontmatter_never_passes_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    schema_path = _make_schema_file(tmp_path, _minimal_schema())
    path = tmp_path / "pursuit.md"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(frontmatter, "pursuit_schema_path", lambda: schema_path)

    errors = validate_pursuit_content(content, schema_path=schema_path)

    assert errors
    result = CliRunner().invoke(frontmatter.cli, ["--validate", "--json", "--file", str(path)])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["status"] == "invalid"
    assert path.read_text(encoding="utf-8") == content
