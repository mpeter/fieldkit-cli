"""Unit tests for `_render_key_value()` null-handling and `write_frontmatter_raw()`
failure paths in `fieldkit.pursuit.io`.

Targets (Track B CRAP cleanup, pursuit/io.py):
    - _render_key_value       (CRAP=15.38, complexity=13)
    - write_frontmatter_raw   (CRAP=15.13, complexity=14)
"""

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from fieldkit.pursuit.io import (
    _render_key_value,
    load_pursuit,
    render_raw_key_value,
    write_frontmatter,
    write_frontmatter_raw,
)
from fieldkit.pursuit.models import PursuitFrontmatter

pytestmark = pytest.mark.unit


def _make_model(**overrides: Any) -> PursuitFrontmatter:
    """Build a minimal valid PursuitFrontmatter, overriding only what a test needs."""
    defaults: dict[str, Any] = {"stage": "qualify"}
    defaults.update(overrides)
    return PursuitFrontmatter(**defaults)


def _make_pursuit_file(tmp_path: Path, content: str, name: str = "acme-corp.md") -> Path:
    """Write a pursuit-style markdown file under tmp_path and return its path."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ── _render_key_value: transition-history null-handling ─────────────────────


def test_render_key_value_transition_history_none_returns_null_line() -> None:
    model = _make_model(transition_history=None)
    result = _render_key_value("transition-history", model, {})
    assert result == ["transition-history: null"]


def test_render_key_value_rejects_historical_data_missing_from_validated_model() -> None:
    model = _make_model()

    with pytest.raises(ValueError, match="missing from the validated model"):
        _render_key_value("meddpicc", model, {"meddpicc": {"champion": 2}})


def test_render_key_value_rejects_non_mapping_historical_data() -> None:
    model = _make_model(legacy_meddpicc={"schema_version": 1, "status": "historical"})

    with pytest.raises(ValueError, match="must be a mapping"):
        _render_key_value("legacy_meddpicc", model, {"legacy_meddpicc": "not-a-map"})


def test_render_raw_key_value_quotes_yaml_syntax_in_scalar_key() -> None:
    rendered = "\n".join(render_raw_key_value("custom: note", "keep"))

    assert yaml.safe_load(rendered) == {"custom: note": "keep"}


def test_typed_write_preserves_yaml_syntax_in_unknown_key(tmp_path: Path) -> None:
    path = _make_pursuit_file(tmp_path, '---\nstage: discover\n"custom: note": keep\n---\nBody\n')
    model, body, mtime = load_pursuit(path)

    write_frontmatter(path, model, body, expected_mtime=mtime)

    parsed = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert parsed["custom: note"] == "keep"


def test_typed_write_round_trips_structured_sf_deal_splits(tmp_path: Path) -> None:
    splits = [{"offering": "Consulting\nPlatform\tPath\\Name", "pct": 0.75}]
    rendered = yaml.safe_dump({"stage": "discover", "sf_deal_splits": splits}, sort_keys=False)
    path = _make_pursuit_file(tmp_path, f"---\n{rendered}---\nBody\n")
    model, body, mtime = load_pursuit(path)

    write_frontmatter(path, model, body, expected_mtime=mtime)

    parsed = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert parsed["sf_deal_splits"] == splits


def test_typed_write_validates_rendered_yaml_before_replacing_file(tmp_path: Path) -> None:
    original = "---\nstage: discover\ncustom: keep\n---\nBody\n"
    path = _make_pursuit_file(tmp_path, original)
    model, body, mtime = load_pursuit(path)

    with (
        patch("fieldkit.pursuit.io._render_key_value", return_value=["broken: ["]),
        pytest.raises(yaml.YAMLError),
    ):
        write_frontmatter(path, model, body, expected_mtime=mtime)

    assert path.read_text(encoding="utf-8") == original


# ── _render_key_value: hyphenated-key null distinction ───────────────────────


def test_render_key_value_hyphenated_key_absent_originally_returns_empty_list() -> None:
    """Model attr None AND raw never had the key -> skip the field entirely (empty list)."""
    model = _make_model(gate_status=None)
    result = _render_key_value("gate-status", model, {})
    assert result == []


def test_render_key_value_hyphenated_key_present_in_raw_but_none_on_model_returns_null_line() -> None:
    """Model attr None but raw HAD a non-None value -> emit an explicit null line.

    Distinct from the "absent originally" case: the key existed on disk and its
    value transitioned to None, so it must still be rendered (not silently dropped).
    """
    model = _make_model(gate_status=None)
    raw = {"gate-status": "was-previously-set"}
    result = _render_key_value("gate-status", model, raw)
    assert result == ["gate-status: null"]


# ── _render_key_value: historical MEDDPICC preservation ────────────────────────


def test_render_key_value_rejects_legacy_mapping_missing_from_model() -> None:
    """Refuse to discard validated historical data during raw rendering."""
    model = _make_model(legacy_meddpicc=None)

    with pytest.raises(ValueError, match="present on disk but missing from the validated model"):
        _render_key_value("legacy_meddpicc", model, {"legacy_meddpicc": {"champion": 7}})


# ── write_frontmatter_raw: create=False failure paths ────────────────────────


def test_write_frontmatter_raw_missing_file_raises_filenotfounderror(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError, match=re.escape(f"write_frontmatter_raw: file not found: {missing}")):
        write_frontmatter_raw(missing, {"stage": "qualify"}, "\n\nbody\n", create=False)


def test_write_frontmatter_raw_exclusive_create_requires_create_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exclusive_create requires create=True"):
        write_frontmatter_raw(tmp_path / "deal.md", {"stage": "qualify"}, "\n", exclusive_create=True)


def test_write_frontmatter_raw_exclusive_create_does_not_replace_racing_file(tmp_path: Path) -> None:
    """Exclusive creation preserves a file created after the initial absence check."""
    path = tmp_path / "deal.md"
    competing_content = "operator-created\n"
    from fieldkit.pursuit import io as pursuit_io

    original_render = pursuit_io.render_frontmatter_raw

    def render_then_create_competing_file(*args: Any, **kwargs: Any) -> str:
        rendered = original_render(*args, **kwargs)
        path.write_text(competing_content, encoding="utf-8")
        return rendered

    with (
        patch("fieldkit.pursuit.io.render_frontmatter_raw", side_effect=render_then_create_competing_file),
        pytest.raises(FileExistsError, match="Pursuit file already exists"),
    ):
        write_frontmatter_raw(path, {"stage": "qualify"}, "\nBody\n", create=True, exclusive_create=True)

    assert path.read_text(encoding="utf-8") == competing_content


def test_write_frontmatter_raw_no_frontmatter_block_reraises_with_new_message(tmp_path: Path) -> None:
    """A file with no '---' delimiters trips _split_frontmatter's ValueError, which is
    re-raised as a new, more specific ValueError (chained via `from exc`, not swallowed).
    """
    path = _make_pursuit_file(tmp_path, "Just plain markdown, no frontmatter at all.\n")

    with pytest.raises(
        ValueError, match=re.escape(f"write_frontmatter_raw: no frontmatter block in {path}")
    ) as exc_info:
        write_frontmatter_raw(path, {"stage": "qualify"}, "\n\nbody\n", create=False)

    # The original _split_frontmatter ValueError is chained as the cause, not lost.
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "delimiters" in str(exc_info.value.__cause__)


@pytest.mark.parametrize(
    ("existing_key", "updated_key"),
    [
        ("legacy_meddpicc", "meddpicc"),
        ("meddpicc", "legacy_meddpicc"),
    ],
)
def test_write_frontmatter_raw_rejects_mixed_legacy_representations(
    tmp_path: Path,
    existing_key: str,
    updated_key: str,
) -> None:
    """Reject updates that combine old and canonical historical score keys."""
    legacy_value = {"schema_version": 1, "status": "historical", "champion": 7}
    existing_value = {"champion": 7} if existing_key == "meddpicc" else legacy_value
    updated_value = {"champion": 7} if updated_key == "meddpicc" else legacy_value
    path = _make_pursuit_file(
        tmp_path,
        "---\n"
        "stage: qualify\n"
        f"{existing_key}:\n"
        + "\n".join(f"  {key}: {value}" for key, value in existing_value.items())
        + "\n---\n\nbody\n",
    )

    with pytest.raises(ValueError, match="mixes meddpicc and legacy_meddpicc representations"):
        write_frontmatter_raw(path, {"stage": "qualify", updated_key: updated_value}, "\n\nbody\n")


def test_write_frontmatter_raw_atomic_failure_cleans_up_temp_file_and_reraises(tmp_path: Path) -> None:
    """If the atomic replace fails, the orphaned temp file is unlinked and the
    original exception propagates unmodified (the `except BaseException` block
    does not swallow it).
    """
    original_content = "---\nstage: qualify\n---\n\nbody\n"
    path = _make_pursuit_file(tmp_path, original_content)

    runtime_root = tmp_path / "runtime"
    with (
        patch("fieldkit.pursuit.io.get_fieldkit_data", return_value=runtime_root),
        patch.object(Path, "replace", side_effect=OSError("simulated replace failure")),
        pytest.raises(OSError, match="simulated replace failure"),
    ):
        write_frontmatter_raw(path, {"stage": "propose"}, "\n\nbody\n", create=False)

    # The failed temp file is cleaned up and the persistent lock stays outside the pursuit directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name not in {path.name, runtime_root.name}]
    assert leftovers == [], f"Expected no orphaned temp files, found: {leftovers}"
    assert not path.with_suffix(f"{path.suffix}.lock").exists()
    assert len(list((runtime_root / "locks" / "pursuit").glob("*.lock"))) == 1
    # The original file is untouched since the replace never completed.
    assert path.read_text(encoding="utf-8") == original_content


# ── write_frontmatter_raw: create=True happy path ────────────────────────────


def test_write_frontmatter_raw_create_true_makes_parent_dirs_and_writes_exact_content(tmp_path: Path) -> None:
    """create=True works for a brand-new path whose parent directories don't exist yet,
    and the resulting file content matches the documented format without
    polluting the pursuit directory with a lock file.
    """
    nested = tmp_path / "nested" / "dir" / "new-pursuit.md"
    assert not nested.parent.exists()

    runtime_root = tmp_path / "runtime"
    with patch("fieldkit.pursuit.io.get_fieldkit_data", return_value=runtime_root):
        write_frontmatter_raw(nested, {"stage": "qualify"}, "\n\nBody text.\n", create=True)

    assert nested.exists()
    assert nested.read_text(encoding="utf-8") == "---\nstage: qualify\n---\n\nBody text.\n"
    leftovers = [p for p in nested.parent.iterdir() if p.name != nested.name]
    assert leftovers == [], f"Expected no leftover temp files, found: {leftovers}"
    assert len(list((runtime_root / "locks" / "pursuit").glob("*.lock"))) == 1
