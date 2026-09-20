"""Tests for fieldkit.pursuit.stale.main — CLI entry point.

Covers the path-resolution branches (bare file, glob-pattern fallback) and the
`found_any`-driven warning aggregation/printing loop that
``test_pursuit_stale.py`` does not exercise (it only covers the no-args error,
directory-glob resolution, template/gmail-intel exclusion, and the final
"no contradictions" message).

``check_file`` is a sibling function in the same module — not a Track B
target — and is patched here so the aggregation/printing logic in ``main()``
can be driven deterministically without real stale-prose content.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.pursuit.stale import main

pytestmark = pytest.mark.unit


def _make_file(directory: Path, name: str, content: str = "irrelevant") -> Path:
    """Create a plain file with the given name under ``directory``."""
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Path resolution — bare file argument (elif p.is_file())
# ---------------------------------------------------------------------------


def test_bare_file_argument_uses_is_file_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct file argument is added via ``elif p.is_file()``.

    Uses a non-``.md`` extension so a directory-glob (``p.glob("*.md")``)
    could never have picked this file up — the only way it reaches
    ``check_file`` is via the bare-file branch.
    """
    target = _make_file(tmp_path, "notes.txt")
    monkeypatch.setattr(sys, "argv", ["cmd", str(target)])

    with patch("fieldkit.pursuit.stale.check_file", return_value=[]) as mock_check:
        main()

    mock_check.assert_called_once_with(str(target))


# ---------------------------------------------------------------------------
# Path resolution — glob pattern fallback (else: Path().glob(arg))
# ---------------------------------------------------------------------------


def test_glob_pattern_fallback_resolves_via_path_glob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An argument that is neither an existing dir nor an existing file falls
    through to ``Path().glob(arg)``.

    ``"*.md"`` is not a literal path that exists, so both ``p.is_dir()`` and
    ``p.is_file()`` are False and resolution must go through the glob
    fallback. Chdir into tmp_path so the glob has a controlled cwd to match
    against.
    """
    monkeypatch.chdir(tmp_path)
    match = _make_file(tmp_path, "match.md")
    monkeypatch.setattr(sys, "argv", ["cmd", "*.md"])

    with patch("fieldkit.pursuit.stale.check_file", return_value=[]) as mock_check:
        main()

    mock_check.assert_called_once_with(match.name)


# ---------------------------------------------------------------------------
# Warning aggregation loop
# ---------------------------------------------------------------------------


def test_no_warnings_prints_nothing_for_only_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``check_file`` returning ``[]`` means nothing is printed for that path
    and ``found_any`` stays False when it is the only path checked."""
    target = _make_file(tmp_path, "pursuit.md")
    monkeypatch.setattr(sys, "argv", ["cmd", str(target)])

    with patch("fieldkit.pursuit.stale.check_file", return_value=[]):
        main()

    out = capsys.readouterr().out
    assert "STALE PROSE WARNINGS" not in out
    assert str(target) not in out
    assert "No stale prose contradictions found." in out


def test_single_path_with_warnings_prints_header_and_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single warning-producing path prints the header once, its own
    ``{path}:`` line, and each warning string."""
    target = _make_file(tmp_path, "pursuit.md")
    monkeypatch.setattr(sys, "argv", ["cmd", str(target)])

    with patch("fieldkit.pursuit.stale.check_file", return_value=["  ⚠ warning text"]):
        main()

    out = capsys.readouterr().out
    assert out.count("STALE PROSE WARNINGS") == 1
    assert f"\n{target}:" in out
    assert "  ⚠ warning text" in out
    assert "No stale prose contradictions found." not in out


def test_two_paths_both_with_warnings_prints_header_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a second path also has warnings, the header still prints exactly
    once — the ``if not found_any:`` guard suppresses the repeat."""
    first = _make_file(tmp_path, "a.md")
    second = _make_file(tmp_path, "b.md")
    monkeypatch.setattr(sys, "argv", ["cmd", str(first), str(second)])

    with patch("fieldkit.pursuit.stale.check_file", return_value=["  ⚠ w"]):
        main()

    out = capsys.readouterr().out
    assert out.count("STALE PROSE WARNINGS") == 1
    assert f"\n{first}:" in out
    assert f"\n{second}:" in out


def test_only_last_of_several_paths_has_warnings_still_prints_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Warnings on the LAST path checked still trigger the header — it is
    driven by ``found_any`` state, not by loop position."""
    first = _make_file(tmp_path, "a.md")
    second = _make_file(tmp_path, "b.md")
    monkeypatch.setattr(sys, "argv", ["cmd", str(first), str(second)])

    def _side_effect(path: str) -> list[str]:
        return ["  ⚠ late warning"] if path == str(second) else []

    with patch("fieldkit.pursuit.stale.check_file", side_effect=_side_effect):
        main()

    out = capsys.readouterr().out
    assert out.count("STALE PROSE WARNINGS") == 1
    assert f"\n{first}:" not in out
    assert f"\n{second}:" in out
    assert "  ⚠ late warning" in out


def test_all_paths_without_warnings_prints_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Multiple paths, none with warnings, still print the final
    "no contradictions" message — a control case alongside the mixed-warning
    tests above."""
    first = _make_file(tmp_path, "a.md")
    second = _make_file(tmp_path, "b.md")
    monkeypatch.setattr(sys, "argv", ["cmd", str(first), str(second)])

    with patch("fieldkit.pursuit.stale.check_file", return_value=[]):
        main()

    out = capsys.readouterr().out
    assert "STALE PROSE WARNINGS" not in out
    assert "No stale prose contradictions found." in out
