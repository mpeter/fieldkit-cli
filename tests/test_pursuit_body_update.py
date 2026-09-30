"""Pursuit body transactions share the existing lock without nested acquisition."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fieldkit.pursuit.io import _pursuit_lock, update_pursuit_body
from fieldkit.util.atomic import PathLockTimeoutError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.pursuit.io.get_fieldkit_data", lambda: tmp_path / "runtime")


def _pursuit(tmp_path: Path) -> Path:
    path = tmp_path / "deal.md"
    path.write_text('---\n# Retain formatting\nstage: "qualify"\n---\n\n## Activity Log\n', encoding="utf-8")
    return path


def test_body_update_preserves_frontmatter_and_mode(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    path.chmod(0o640)
    assert update_pursuit_body(path, lambda body: body + "- Meeting\n") is True
    assert path.read_text(encoding="utf-8") == (
        '---\n# Retain formatting\nstage: "qualify"\n---\n\n## Activity Log\n- Meeting\n'
    )
    assert path.stat().st_mode & 0o777 == 0o640


def test_body_noop_does_not_replace_file(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    before = path.stat()
    assert update_pursuit_body(path, lambda body: body) is False
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_body_update_uses_existing_pursuit_lock(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    original = path.read_bytes()
    with _pursuit_lock(path), pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"):
        update_pursuit_body(path, lambda body: body + "- Meeting\n", timeout_seconds=0)
    assert path.read_bytes() == original


def test_body_transform_failure_leaves_file_unchanged(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    original = path.read_bytes()

    def fail(body: str) -> str:
        raise ValueError("Ownership conflict")

    with pytest.raises(ValueError, match="Ownership conflict"):
        update_pursuit_body(path, fail)
    assert path.read_bytes() == original


def test_body_update_cannot_swallow_frontmatter_delimiter(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Pursuit body must begin with a newline"):
        update_pursuit_body(path, lambda body: "Not a separate body")
    assert path.read_bytes() == original


def test_missing_pursuit_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.md"
    with pytest.raises(FileNotFoundError, match=r"missing\.md"):
        update_pursuit_body(path, lambda body: body + "- Meeting\n")
    assert not path.exists()


def test_concurrent_body_updates_preserve_all_entries(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)

    def append(index: int) -> bool:
        return update_pursuit_body(path, lambda body: body + f"- Meeting {index}\n")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(append, range(12)))
    assert results == [True] * 12
    content = path.read_text(encoding="utf-8")
    assert all(content.count(f"- Meeting {index}\n") == 1 for index in range(12))


def test_noncooperating_edit_is_not_overwritten(tmp_path: Path) -> None:
    path = _pursuit(tmp_path)
    edited = '---\nstage: "qualify"\n---\nHuman edit\n'

    def race(body: str) -> str:
        path.write_text(edited, encoding="utf-8")
        return body + "- Meeting\n"

    with pytest.raises(ValueError, match="Pursuit changed during body update"):
        update_pursuit_body(path, race)
    assert path.read_text(encoding="utf-8") == edited
