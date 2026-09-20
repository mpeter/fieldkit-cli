"""Unit tests for fieldkit.skill.template.install_skill_dir().

All tests use tmp_path for filesystem isolation and make no LLM or network calls.
Mirrors test_skill_install_flat.py but exercises the directory-format installer.
"""

from pathlib import Path

import pytest

from fieldkit.skill.template import install_skill_dir

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill_dir(base: Path, skill_md_content: str = "Hello {{name}}") -> Path:
    """Create a minimal skill directory with a SKILL.md under *base*."""
    skill_dir = base / "my-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# 1. Renders template variables and writes to target
# ---------------------------------------------------------------------------


def test_install_skill_dir_writes_rendered_content(tmp_path: Path) -> None:
    """install_skill_dir renders {{name}} and writes the result to target_skill_dir/SKILL.md."""
    skill_dir = _make_skill_dir(tmp_path, "Hello {{name}}")
    target_dir = tmp_path / "out" / "my-skill"

    result = install_skill_dir(skill_dir, target_dir, {"name": "World"})

    assert result.rendered == 1
    assert result.errors == 0
    out_file = target_dir / "SKILL.md"
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == "Hello World"


# ---------------------------------------------------------------------------
# 2. Creates parent directory when it does not exist
# ---------------------------------------------------------------------------


def test_install_skill_dir_creates_parent_dir(tmp_path: Path) -> None:
    """install_skill_dir creates the target directory (and parents) if absent."""
    skill_dir = _make_skill_dir(tmp_path, "# Skill content")
    # Deeply nested path whose parents do not exist yet
    target_dir = tmp_path / "a" / "b" / "c" / "my-skill"

    assert not target_dir.exists()

    result = install_skill_dir(skill_dir, target_dir, {})

    assert result.errors == 0
    assert target_dir.is_dir()
    out_file = target_dir / "SKILL.md"
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == "# Skill content"


# ---------------------------------------------------------------------------
# 3. Dry-run: no file written, result.rendered == 1
# ---------------------------------------------------------------------------


def test_install_skill_dir_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """dry_run=True prints the planned action but does not write any file."""
    skill_dir = _make_skill_dir(tmp_path, "Hello {{name}}")
    target_dir = tmp_path / "out" / "my-skill"

    result = install_skill_dir(skill_dir, target_dir, {"name": "World"}, dry_run=True)

    # File must NOT be written
    assert not target_dir.exists()
    # No errors
    assert result.errors == 0
    # Dry-run output must be present on stdout
    out = capsys.readouterr().out
    assert "[dry-run]" in out


# ---------------------------------------------------------------------------
# 4. Missing SKILL.md → errors == 1
# ---------------------------------------------------------------------------


def test_install_skill_dir_missing_skill_md(tmp_path: Path) -> None:
    """install_skill_dir returns errors=1 when SKILL.md is absent from skill_dir."""
    skill_dir = tmp_path / "empty-skill"
    skill_dir.mkdir()
    # Deliberately do NOT create SKILL.md

    target_dir = tmp_path / "out" / "empty-skill"

    result = install_skill_dir(skill_dir, target_dir, {})

    assert result.errors == 1
    assert result.rendered == 0
    # Target directory must not be created
    assert not target_dir.exists()


# ---------------------------------------------------------------------------
# 5. Non-.md file is copied verbatim
# ---------------------------------------------------------------------------


def test_install_skill_dir_non_md_file_copied(tmp_path: Path) -> None:
    """install_skill_dir copies non-.md files verbatim into the target directory."""
    skill_dir = _make_skill_dir(tmp_path, "# Skill")
    # Add a non-.md file (e.g. evals.json)
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    evals_content = '{"evals": []}'
    (evals_dir / "evals.json").write_text(evals_content, encoding="utf-8")

    target_dir = tmp_path / "out" / "my-skill"

    result = install_skill_dir(skill_dir, target_dir, {})

    assert result.errors == 0
    assert result.rendered == 1  # SKILL.md
    assert result.copied == 1  # evals.json
    copied_file = target_dir / "evals" / "evals.json"
    assert copied_file.exists()
    assert copied_file.read_text(encoding="utf-8") == evals_content


# ---------------------------------------------------------------------------
# 6. Replaces symlink at target with a real directory
# ---------------------------------------------------------------------------


def test_install_skill_dir_replaces_symlink(tmp_path: Path) -> None:
    """install_skill_dir unlinks an existing symlink at target_skill_dir before writing."""
    skill_dir = _make_skill_dir(tmp_path, "# Skill")
    target_dir = tmp_path / "out" / "my-skill"

    # Create a symlink at the target location pointing to a dummy directory
    dummy = tmp_path / "dummy"
    dummy.mkdir()
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    target_dir.symlink_to(dummy)
    assert target_dir.is_symlink()

    result = install_skill_dir(skill_dir, target_dir, {})

    assert result.errors == 0
    # The symlink must be replaced with a real directory containing SKILL.md
    assert not target_dir.is_symlink()
    assert (target_dir / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# 7. OSError on write → errors == 1, no exception propagates
# ---------------------------------------------------------------------------


def test_install_skill_dir_oserror_on_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install_skill_dir handles OSError on write_text gracefully (errors=1, no raise).

    Uses monkeypatch to inject an OSError on Path.write_text so the test is
    reliable regardless of whether the process runs as root.
    """
    skill_dir = _make_skill_dir(tmp_path, "# Content")
    target_dir = tmp_path / "out" / "my-skill"

    original_write_text = Path.write_text

    def _raise_on_skill_md(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "SKILL.md" and "out" in str(self):
            raise OSError("Permission denied")
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _raise_on_skill_md)

    # Must not raise — errors are counted, not propagated
    result = install_skill_dir(skill_dir, target_dir, {})

    assert result.errors == 1
    assert result.rendered == 0
