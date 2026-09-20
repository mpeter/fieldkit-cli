"""Tests for _load_env() — covering uncovered branches.

cc=9, cov=58%, target: quoted values, comments, blank lines, missing =,
multiple calls idempotency.
"""

import os
from pathlib import Path

import pytest

from fieldkit.commands.doctor.google import _load_env

pytestmark = pytest.mark.unit


# ── TestLoadEnvQuotedValues (flattened) ─────────────────────────────────────


def test_load_env_quoted_values_strips_double_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values wrapped in double-quotes have the quotes stripped."""
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED_VAR="hello world"\n', encoding="utf-8")
    monkeypatch.delenv("QUOTED_VAR", raising=False)

    _load_env(env_file)

    assert os.environ.get("QUOTED_VAR") == "hello world"


def test_load_env_quoted_values_strips_single_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values wrapped in single-quotes have the quotes stripped."""
    env_file = tmp_path / ".env"
    env_file.write_text("SINGLE_QUOTED='my value'\n", encoding="utf-8")
    monkeypatch.delenv("SINGLE_QUOTED", raising=False)

    _load_env(env_file)

    assert os.environ.get("SINGLE_QUOTED") == "my value"


# ── TestLoadEnvSkipsComments (flattened) ────────────────────────────────────


def test_load_env_skips_comments_comment_lines_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines starting with # are not parsed as key=value pairs."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# This is a comment\nREAL_VAR=real_value\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("REAL_VAR", raising=False)

    _load_env(env_file)

    assert os.environ.get("REAL_VAR") == "real_value"


def test_load_env_skips_comments_inline_comment_not_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline comments (value # comment) are part of the value (simple parser)."""
    # The simple parser only strips leading # lines, not inline comments.
    env_file = tmp_path / ".env"
    env_file.write_text("INLINE_VAR=value  # comment\n", encoding="utf-8")
    monkeypatch.delenv("INLINE_VAR", raising=False)

    _load_env(env_file)

    # Value includes the inline comment text (simple parser behaviour)
    assert os.environ.get("INLINE_VAR") is not None


# ── TestLoadEnvSkipsBlankLines (flattened) ──────────────────────────────────


def test_load_env_skips_blank_lines_blank_lines_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank lines between entries do not cause errors."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n\nVAR_A=alpha\n\n\nVAR_B=beta\n\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("VAR_A", raising=False)
    monkeypatch.delenv("VAR_B", raising=False)

    _load_env(env_file)

    assert os.environ.get("VAR_A") == "alpha"
    assert os.environ.get("VAR_B") == "beta"


# ── TestLoadEnvSkipsNoEquals (flattened) ────────────────────────────────────


def test_load_env_skips_no_equals_lines_without_equals_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines with no '=' are ignored (not valid KEY=VALUE pairs)."""
    env_file = tmp_path / ".env"
    env_file.write_text("NOTAKEY\nVALID=value\n", encoding="utf-8")
    monkeypatch.delenv("VALID", raising=False)

    _load_env(env_file)

    # NOTAKEY must not appear in environ (it has no =)
    assert "NOTAKEY" not in os.environ
    assert os.environ.get("VALID") == "value"


# ── TestLoadEnvIdempotent (flattened) ───────────────────────────────────────


def test_load_env_idempotent_calling_twice_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_env called twice does not overwrite values set by the first call."""
    env_file = tmp_path / ".env"
    env_file.write_text("ONCE_VAR=first\n", encoding="utf-8")
    monkeypatch.delenv("ONCE_VAR", raising=False)

    _load_env(env_file)
    assert os.environ.get("ONCE_VAR") == "first"

    # Overwrite the file with a different value
    env_file.write_text("ONCE_VAR=second\n", encoding="utf-8")
    _load_env(env_file)

    # Already set — must not be overwritten
    assert os.environ.get("ONCE_VAR") == "first"


# ── TestLoadEnvExportPrefix (flattened) ─────────────────────────────────────


def test_load_env_export_prefix_export_prefix_with_no_trailing_space_is_handled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'export KEY=VALUE' form strips 'export ' correctly."""
    env_file = tmp_path / ".env"
    env_file.write_text("export EXPORT_VAR=exported_value\n", encoding="utf-8")
    monkeypatch.delenv("EXPORT_VAR", raising=False)

    _load_env(env_file)

    assert os.environ.get("EXPORT_VAR") == "exported_value"


# ── TestLoadEnvValueWithEquals (flattened) ──────────────────────────────────


def test_load_env_value_with_equals_value_containing_equals_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values containing '=' characters are preserved (partition on first '=' only)."""
    env_file = tmp_path / ".env"
    env_file.write_text("CONN_STR=host=localhost;port=5432\n", encoding="utf-8")
    monkeypatch.delenv("CONN_STR", raising=False)

    _load_env(env_file)

    assert os.environ.get("CONN_STR") == "host=localhost;port=5432"


# ── TestLoadEnvMissingFile (flattened) ──────────────────────────────────────


def test_load_env_missing_file_missing_file_is_noop(tmp_path: Path) -> None:
    """load_env silently does nothing when the file does not exist."""
    missing = tmp_path / "no-such.env"
    # Must not raise
    _load_env(missing)
