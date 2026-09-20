"""Unit tests for duplicate-key detection in lib/io.py."""

import logging
import textwrap
from pathlib import Path

import pytest

from fieldkit.pursuit.io import load_pursuit, write_frontmatter

pytestmark = pytest.mark.unit

# A pursuit file with a duplicate 'stage' key.
DUPLICATE_KEY_CONTENT = textwrap.dedent("""\
    ---
    stage: discover
    stage: qualify
    ---
    Some body text.
""")

# A clean pursuit file with no duplicates.
CLEAN_CONTENT = textwrap.dedent("""\
    ---
    stage: discover
    ---
    Some body text.
""")


def test_load_duplicate_key_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Loading YAML with duplicate keys emits a WARNING and last-value is returned."""
    f = tmp_path / "pursuit.md"
    f.write_text(DUPLICATE_KEY_CONTENT, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fieldkit.pursuit.io"):
        fm, _body, _mtime = load_pursuit(f)

    # At least one WARNING mentioning the duplicate key
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "Expected a WARNING log for duplicate key"
    assert any("stage" in r.message for r in warnings), (
        f"Expected duplicate-key warning mentioning 'stage', got: {[r.message for r in warnings]}"
    )

    # Last-value semantics: 'stage' should be 'qualify' (the second occurrence)
    assert fm.stage == "qualify"


def test_write_frontmatter_duplicate_keys_raises_and_leaves_file_unchanged(tmp_path: Path) -> None:
    """write_frontmatter on a file with duplicate keys raises ValueError; file is unchanged."""
    f = tmp_path / "pursuit.md"
    f.write_text(DUPLICATE_KEY_CONTENT, encoding="utf-8")
    original_content = f.read_text(encoding="utf-8")

    # Load to get a valid model and body for the write call
    fm, body, mtime = load_pursuit(f)
    # Mutate the model to a different stage to confirm the write never happens
    fm_updated = fm.model_copy(update={"stage": "propose"})

    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        write_frontmatter(f, fm_updated, body, expected_mtime=mtime)

    assert f.read_text(encoding="utf-8") == original_content, "File must be unchanged after ValueError"


def test_load_clean_yaml_no_warnings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Loading clean YAML emits no duplicate-key warnings."""
    f = tmp_path / "pursuit.md"
    f.write_text(CLEAN_CONTENT, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fieldkit.pursuit.io"):
        fm, _body, _mtime = load_pursuit(f)

    dup_warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "uplicate" in r.message]
    assert not dup_warnings, f"Unexpected duplicate-key warnings: {[r.message for r in dup_warnings]}"
    assert fm.stage == "discover"
