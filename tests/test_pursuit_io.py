"""Unit tests for parse_frontmatter() in fieldkit.pursuit.io."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import IO, Any
from unittest.mock import patch

import pytest

from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit import write_frontmatter_raw
from fieldkit.pursuit.io import (
    _pursuit_lock,
    load_pursuit,
    parse_frontmatter,
    parse_frontmatter_fallback,
    write_frontmatter,
)


@pytest.mark.unit
def test_parse_frontmatter_valid_returns_dict_and_body() -> None:
    """Valid frontmatter block returns (dict, body) with correct values."""
    text = "---\nstage: qualify\nname: Acme Deal\n---\n\nBody content here.\n"
    result = parse_frontmatter(text)
    assert result is not None
    fm, body = result
    assert fm["stage"] == "qualify"
    assert fm["name"] == "Acme Deal"
    assert "Body content here." in body


@pytest.mark.unit
def test_parse_frontmatter_missing_closing_delimiter_returns_none() -> None:
    """Text with opening --- but no closing --- returns None."""
    text = "---\nstage: qualify\nname: Acme Deal\n"
    result = parse_frontmatter(text)
    assert result is None


@pytest.mark.unit
def test_parse_frontmatter_no_frontmatter_returns_none() -> None:
    """Plain text with no frontmatter delimiters returns None."""
    text = "Just some markdown text\nwith no frontmatter at all.\n"
    result = parse_frontmatter(text)
    assert result is None


@pytest.mark.unit
@pytest.mark.parametrize("frontmatter", ["- sf-opportunity-id", "not-a-mapping", "42"])
def test_parse_frontmatter_fallback_rejects_non_mapping_yaml(frontmatter: str) -> None:
    parsed, body = parse_frontmatter_fallback(f"---\n{frontmatter}\n---\nBody\n")

    assert parsed is None
    assert body


@pytest.mark.unit
def test_parse_frontmatter_empty_body_returns_empty_string() -> None:
    """Frontmatter-only file (no body) returns (dict, '') or (dict, '\\n')."""
    text = "---\nstage: qualify\n---\n"
    result = parse_frontmatter(text)
    assert result is not None
    fm, body = result
    assert fm["stage"] == "qualify"
    # Body may be empty string or just a newline — both are acceptable
    assert body.strip() == ""


@pytest.mark.unit
def test_parse_frontmatter_duplicate_keys_returns_last_value(caplog: pytest.LogCaptureFixture) -> None:
    """Duplicate YAML keys return a dict (last value wins) without raising."""
    text = "---\nstage: qualify\nstage: negotiate\n---\n\nBody.\n"
    import logging

    with caplog.at_level(logging.WARNING, logger="fieldkit.pursuit.io"):
        result = parse_frontmatter(text)
    assert result is not None
    fm, _ = result
    # Last value wins per _DuplicateKeyLoader
    assert fm["stage"] == "negotiate"
    # Warning should have been emitted
    assert any("Duplicate" in r.message for r in caplog.records)


@pytest.mark.unit
def test_parse_frontmatter_invalid_yaml_returns_none() -> None:
    """Syntactically invalid YAML frontmatter returns None (no exception raised)."""
    # Tabs are invalid in YAML indentation
    text = "---\nkey: value\n\tinvalid: tab-indented\n---\n\nBody.\n"
    result = parse_frontmatter(text)
    assert result is None


@pytest.mark.unit
def test_parse_frontmatter_non_dict_yaml_returns_none() -> None:
    """YAML that parses to a non-dict (e.g. a list) returns None."""
    text = "---\n- item1\n- item2\n---\n\nBody.\n"
    result = parse_frontmatter(text)
    assert result is None


# ── FrontmatterStalenessError tests (historic regression / Spec 021b) ────────────────────


@pytest.mark.unit
def test_typed_and_raw_frontmatter_writers_refuse_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("---\nstage: qualify\n---\n\nunchanged\n", encoding="utf-8")
    model, body, mtime = load_pursuit(target)
    link = tmp_path / "link.md"
    link.symlink_to(target)

    with pytest.raises(OSError, match="Refusing to replace symlinked pursuit file"):
        write_frontmatter(link, model, body, expected_mtime=mtime)
    with pytest.raises(OSError, match="Refusing to replace symlinked pursuit file"):
        write_frontmatter_raw(link, {"stage": "discover"}, body, expected_mtime=mtime)

    assert target.read_text(encoding="utf-8") == "---\nstage: qualify\n---\n\nunchanged\n"


@pytest.mark.unit
def test_write_frontmatter_raw_staleness_error_on_mtime_mismatch(tmp_path: Path) -> None:
    """FrontmatterStalenessError raised when file modified since last read."""
    path = tmp_path / "test.md"
    path.write_text("---\nstage: qualify\n---\n\nbody\n", encoding="utf-8")
    stale_mtime = path.stat().st_mtime - 10.0  # old mtime
    with pytest.raises(FrontmatterStalenessError) as exc_info:
        write_frontmatter_raw(path, {"stage": "propose"}, "\n\nbody\n", expected_mtime=stale_mtime)
    assert str(path) in str(exc_info.value)


@pytest.mark.unit
def test_write_frontmatter_raw_correct_mtime_succeeds(tmp_path: Path) -> None:
    """Correct mtime passes the staleness guard."""
    path = tmp_path / "test.md"
    path.write_text("---\nstage: qualify\n---\n\nbody\n", encoding="utf-8")
    correct_mtime = path.stat().st_mtime
    write_frontmatter_raw(path, {"stage": "propose"}, "\n\nbody\n", expected_mtime=correct_mtime)


@pytest.mark.unit
def test_write_frontmatter_raw_serializes_check_and_replace(tmp_path: Path) -> None:
    path = tmp_path / "test.md"
    path.write_text("---\nstage: qualify\n---\n\nbody\n", encoding="utf-8")
    expected_mtime = path.stat().st_mtime
    started = Event()
    finished = Event()

    def stale_writer() -> None:
        started.set()
        try:
            write_frontmatter_raw(path, {"stage": "propose"}, "\n\nbody\n", expected_mtime=expected_mtime)
        finally:
            finished.set()

    runtime_root = tmp_path / "runtime"
    with patch("fieldkit.pursuit.io.get_fieldkit_data", return_value=runtime_root):
        with ThreadPoolExecutor(max_workers=1) as pool, _pursuit_lock(path):
            future = pool.submit(stale_writer)
            assert started.wait(timeout=1)
            assert not finished.wait(timeout=0.1)
            path.write_text("---\nstage: discover\n---\n\nconcurrent\n", encoding="utf-8")

        with pytest.raises(FrontmatterStalenessError, match="modified since last read"):
            future.result()
    assert "concurrent" in path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_load_pursuit_returns_content_and_mtime_from_same_open_file(tmp_path: Path) -> None:
    path = tmp_path / "deal.md"
    original = "---\nstage: qualify\n---\n\noriginal\n"
    concurrent = "---\nstage: discover\n---\n\nconcurrent\n"
    path.write_text(original, encoding="utf-8")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    original_open = Path.open

    class RacingReader:
        def __init__(self, source: IO[str]) -> None:
            self.source = source

        def __enter__(self) -> "RacingReader":
            return self

        def __exit__(self, *args: object) -> None:
            self.source.close()

        def read(self) -> str:
            content = self.source.read()
            replacement = tmp_path / "replacement.md"
            replacement.write_text(concurrent, encoding="utf-8")
            os.utime(replacement, (1_700_000_100, 1_700_000_100))
            replacement.replace(path)
            return content

        def fileno(self) -> int:
            return self.source.fileno()

    def racing_open(target: Path, *args: Any, **kwargs: Any) -> IO[str] | RacingReader:
        source = original_open(target, *args, **kwargs)
        return RacingReader(source) if target == path else source

    with patch.object(Path, "open", racing_open):
        model, body, loaded_mtime = load_pursuit(path)

    assert model.stage == "qualify"
    assert "original" in body
    assert loaded_mtime == 1_700_000_000
    assert path.stat().st_mtime == 1_700_000_100


@pytest.mark.unit
def test_staleness_error_is_fieldkiterror_subclass() -> None:
    """FrontmatterStalenessError is a FieldkitError subclass (re-parented from ValueError, #1169)."""
    from fieldkit.errors import FieldkitError

    assert issubclass(FrontmatterStalenessError, FieldkitError)
    assert not issubclass(FrontmatterStalenessError, ValueError)


# ── historic regression: _yaml_scalar "---" quoting ──────────────────────────────────────


@pytest.mark.unit
def test_yaml_scalar_quotes_triple_dash_value() -> None:
    """Values containing '---' must be quoted to avoid YAML document-end confusion."""
    from fieldkit.pursuit.io import _yaml_scalar

    result = _yaml_scalar("plan --- execute")
    assert result.startswith('"'), f"Expected quoted output, got: {result!r}"
    assert "plan --- execute" in result


@pytest.mark.unit
def test_yaml_scalar_quotes_carriage_return() -> None:
    """Values containing '\\r' must be quoted."""
    from fieldkit.pursuit.io import _yaml_scalar

    result = _yaml_scalar("line1\rline2")
    assert result.startswith('"'), f"Expected quoted output for \\r, got: {result!r}"


@pytest.mark.unit
def test_yaml_scalar_quotes_null_byte() -> None:
    """Values containing null byte must be quoted."""
    from fieldkit.pursuit.io import _yaml_scalar

    result = _yaml_scalar("before\x00after")
    assert result.startswith('"'), f"Expected quoted output for \\x00, got: {result!r}"


# ── write_frontmatter_raw create=True tests ───────────────────────────────────


@pytest.mark.unit
def test_write_frontmatter_raw_create_new_file(tmp_path: Path) -> None:
    """create=True writes a new file that does not yet exist."""
    path = tmp_path / "new.md"
    assert not path.exists()
    write_frontmatter_raw(path, {"stage": "qualify", "title": "New Deal"}, "\n\nbody\n", create=True)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "stage: qualify" in content
    assert "title: New Deal" in content


@pytest.mark.unit
def test_write_frontmatter_raw_create_preserves_dict_order(tmp_path: Path) -> None:
    """create=True preserves dict insertion order (not alphabetical)."""
    path = tmp_path / "new.md"
    write_frontmatter_raw(path, {"sf_stage": "qualify", "title": "Acme", "sf_arr": 50000}, "\n\nbody\n", create=True)
    content = path.read_text(encoding="utf-8")
    # sf_stage must come before title, which must come before sf_arr
    assert content.index("sf_stage:") < content.index("title:") < content.index("sf_arr:")


# ── _yaml_scalar additional coverage (CRAP reduction) ────────────────────────


# ── TestYamlScalarAdditional (flattened) ────────────────────────────────────


def _yaml_scalar_additional_scalar(val: object) -> str:
    from fieldkit.pursuit.io import _yaml_scalar

    return _yaml_scalar(val)


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_none_returns_null() -> None:
    assert _yaml_scalar_additional_scalar(None) == "null"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_true_returns_true() -> None:
    assert _yaml_scalar_additional_scalar(True) == "true"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_false_returns_false() -> None:
    assert _yaml_scalar_additional_scalar(False) == "false"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_integer_returns_string_repr() -> None:
    assert _yaml_scalar_additional_scalar(42) == "42"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_float_returns_string_repr() -> None:
    assert _yaml_scalar_additional_scalar(3.14) == "3.14"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_empty_string_returns_quoted_empty() -> None:
    assert _yaml_scalar_additional_scalar("") == '""'


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_string_starting_with_brace_is_quoted() -> None:
    result = _yaml_scalar_additional_scalar("{key: val}")
    assert result.startswith('"'), f"Expected quoted, got: {result!r}"
    assert "key: val" in result


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_string_starting_with_bracket_is_quoted() -> None:
    result = _yaml_scalar_additional_scalar("[item1, item2]")
    assert result.startswith('"'), f"Expected quoted, got: {result!r}"


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_string_with_colon_space_is_quoted() -> None:
    result = _yaml_scalar_additional_scalar("key: value")
    assert result.startswith('"'), f"Expected quoted for colon-space, got: {result!r}"
    assert "key: value" in result


@pytest.mark.unit
@pytest.mark.unit
def test_yaml_scalar_additional_normal_string_returned_unquoted() -> None:
    result = _yaml_scalar_additional_scalar("ordinary string")
    assert result == "ordinary string"


@pytest.mark.unit
def test_yaml_scalar_isoformat_object() -> None:
    """Objects with isoformat() method (date, datetime) are rendered via isoformat."""
    from datetime import date as _date

    from fieldkit.pursuit.io import _yaml_scalar

    d = _date(2026, 6, 24)
    assert _yaml_scalar(d) == "2026-06-24"


@pytest.mark.unit
def test_yaml_scalar_unknown_type_uses_str() -> None:
    """Unknown types fall through to str()."""
    from fieldkit.pursuit.io import _yaml_scalar

    class _Custom:
        def __str__(self) -> str:
            return "custom-repr"

    assert _yaml_scalar(_Custom()) == "custom-repr"


@pytest.mark.unit
def test_yaml_needs_quote_exported() -> None:
    """_yaml_needs_quote is importable and correct."""
    from fieldkit.pursuit.io import _yaml_needs_quote

    assert _yaml_needs_quote(": value") is True
    assert _yaml_needs_quote("normal") is False
    assert _yaml_needs_quote("true") is True  # boolean keyword
    assert _yaml_needs_quote("---") is True  # YAML separator


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,needs_quote",
    [
        ("71721820", True),  # digit-only opportunity number → would load as int
        ("00123", True),  # leading zero, still numeric to YAML
        ("-5", True),  # signed int
        ("3.14", True),  # float
        ("1e6", True),  # scientific notation
        ("v2", False),  # has a non-numeric char → safe unquoted
        ("2026-06-24", True),  # YAML would load an unquoted date-like string as datetime.date
    ],
)
def test_yaml_needs_quote_retyped_strings(value: str, needs_quote: bool) -> None:
    """Strings that YAML would re-type must be quoted to preserve their semantic value."""
    from fieldkit.pursuit.io import _yaml_needs_quote

    assert _yaml_needs_quote(value) is needs_quote


@pytest.mark.unit
def test_opportunity_number_round_trips_as_string(tmp_path: Path) -> None:
    """A digit-only sf_opportunity_number survives write→load as a str, not an int.

    Regression (CRITICAL, implementation change review): an unquoted digit-only scalar
    (``sf_opportunity_number: 71721820``) loads back through yaml.load as an
    int, and the ``str | None`` field then fails Pydantic validation — making
    every synced pursuit file unloadable. The write path must quote it.
    """
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    path = tmp_path / "acme.md"
    path.write_text('---\nstage: qualify\nsf_opportunity_number: ""\n---\n\nBody.\n', encoding="utf-8")

    model, body, _mtime = load_pursuit(path)
    updated = model.model_copy(update={"sf_opportunity_number": "71721820"})
    write_frontmatter(path, updated, body)

    # On-disk scalar must be quoted so YAML does not re-type it as int.
    assert 'sf_opportunity_number: "71721820"' in path.read_text(encoding="utf-8")

    # And it must load back as the original string without raising.
    reloaded, _body2, _mtime2 = load_pursuit(path)
    assert reloaded.sf_opportunity_number == "71721820"
    assert isinstance(reloaded.sf_opportunity_number, str)


@pytest.mark.unit
def test_load_pursuit_coerces_bare_int_opportunity_number_to_str(tmp_path: Path) -> None:
    """A legacy/hand-edited bare-int sf_opportunity_number loads as str, not a ValidationError."""
    from fieldkit.pursuit.io import load_pursuit

    path = tmp_path / "legacy.md"
    path.write_text("---\nstage: qualify\nsf_opportunity_number: 71721820\n---\n\nBody.\n", encoding="utf-8")

    model, _body, _mtime = load_pursuit(path)
    assert model.sf_opportunity_number == "71721820"
    assert isinstance(model.sf_opportunity_number, str)
