"""Exclusive atomic publication never replaces a peer's destination."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.util.atomic import atomic_text_create

pytestmark = pytest.mark.unit


def test_atomic_create_publishes_complete_private_file(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    assert atomic_text_create(path, "Complete\n") is None
    assert path.read_text(encoding="utf-8") == "Complete\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_create_refuses_existing_destination(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("Other writer", encoding="utf-8")
    with pytest.raises(FileExistsError, match="File exists"):
        atomic_text_create(path, "Replacement")
    assert path.read_text(encoding="utf-8") == "Other writer"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_create_cleans_temporary_file_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    with (
        patch("fieldkit.util.atomic.os.link", side_effect=OSError("link failed")),
        pytest.raises(OSError, match="link failed"),
    ):
        atomic_text_create(path, "Complete")
    assert list(tmp_path.iterdir()) == []
