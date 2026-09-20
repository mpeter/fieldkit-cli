"""Unit tests for fieldkit.skill.template.install_skill_flat().

All tests use tmp_path for filesystem isolation and make no LLM or network calls.
"""

from pathlib import Path

import pytest

from fieldkit.skill.template import install_skill_flat

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


def test_install_skill_flat_writes_rendered_content(tmp_path: Path) -> None:
    """install_skill_flat renders {{name}} and writes the result to target_file."""
    skill_dir = _make_skill_dir(tmp_path, "Hello {{name}}")
    target_file = tmp_path / "out" / "my-skill.md"

    result = install_skill_flat(skill_dir, target_file, {"name": "World"})

    assert result.rendered == 1
    assert result.errors == 0
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "Hello World"


def test_install_skill_flat_embeds_local_markdown_support(tmp_path: Path) -> None:
    """Cursor's one-file target retains local Markdown support instructions."""
    skill_dir = _make_skill_dir(
        tmp_path,
        "# Brief\n\nRead [the week-start instructions](ops/week-start.md).\n",
    )
    support = skill_dir / "ops"
    support.mkdir()
    (support / "week-start.md").write_text("# Week start\n\nPrepare the week.\n", encoding="utf-8")
    target_file = tmp_path / "out" / "brief.md"

    result = install_skill_flat(skill_dir, target_file, {})

    installed = target_file.read_text(encoding="utf-8")
    assert result.rendered == 1
    assert result.errors == 0
    assert "](#fieldkit-support-ops-week-start-md)" in installed
    assert '<a id="fieldkit-support-ops-week-start-md"></a>' in installed
    assert "Prepare the week." in installed


# ---------------------------------------------------------------------------
# 2. Creates parent directory when it does not exist
# ---------------------------------------------------------------------------


def test_install_skill_flat_creates_parent_dir(tmp_path: Path) -> None:
    """install_skill_flat creates the target_file's parent directory if absent."""
    skill_dir = _make_skill_dir(tmp_path, "# Skill content")
    # Deeply nested path whose parents do not exist yet
    target_file = tmp_path / "a" / "b" / "c" / "skill.md"

    assert not target_file.parent.exists()

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 0
    assert target_file.parent.is_dir()
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "# Skill content"


# ---------------------------------------------------------------------------
# 3. Dry-run: no file written, result.rendered == 1
# ---------------------------------------------------------------------------


def test_install_skill_flat_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """dry_run=True prints the planned action but does not write any file."""
    skill_dir = _make_skill_dir(tmp_path, "Hello {{name}}")
    target_file = tmp_path / "out" / "my-skill.md"

    result = install_skill_flat(skill_dir, target_file, {"name": "World"}, dry_run=True)

    # File must NOT be written
    assert not target_file.exists()
    # Result still counts the planned render
    assert result.rendered == 1
    assert result.errors == 0
    # Dry-run output must be present on stdout
    out = capsys.readouterr().out
    assert "[dry-run]" in out


# ---------------------------------------------------------------------------
# 4. Non-.md files in skill_dir are ignored
# ---------------------------------------------------------------------------


def test_install_skill_flat_non_md_ignored(tmp_path: Path) -> None:
    """install_skill_flat only processes SKILL.md; evals/ and other files are ignored."""
    skill_dir = _make_skill_dir(tmp_path, "# Only this")
    # Add a non-.md file that must NOT appear at the target location
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    (evals_dir / "evals.json").write_text('{"evals": []}', encoding="utf-8")

    target_file = tmp_path / "out" / "my-skill.md"

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 0
    assert result.rendered == 1
    # Target file contains only SKILL.md content
    assert target_file.read_text(encoding="utf-8") == "# Only this"
    # evals.json must NOT appear next to the target
    assert not (tmp_path / "out" / "evals" / "evals.json").exists()
    assert not (tmp_path / "out" / "evals.json").exists()


# ---------------------------------------------------------------------------
# 5. Missing SKILL.md → errors == 1, target not created
# ---------------------------------------------------------------------------


def test_install_skill_flat_missing_skill_md(tmp_path: Path) -> None:
    """install_skill_flat returns errors=1 when SKILL.md is absent from skill_dir."""
    skill_dir = tmp_path / "empty-skill"
    skill_dir.mkdir()
    # Deliberately do NOT create SKILL.md

    target_file = tmp_path / "out" / "empty-skill.md"

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 1
    assert result.rendered == 0
    # Target file must not be created
    assert not target_file.exists()


def test_install_skill_flat_reports_markdown_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundled Markdown read error is reported without creating the target."""
    skill_dir = _make_skill_dir(tmp_path, "# Content")
    target_file = tmp_path / "out" / "my-skill.md"

    def _raise_read_error(*_args: object, **_kwargs: object) -> tuple[str, list[object]]:
        raise OSError("read denied")

    monkeypatch.setattr("fieldkit.skill.template._render_flat_skill_bundle", _raise_read_error)

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 1
    assert result.rendered == 0
    assert target_file.exists() is False


def test_install_skill_flat_reports_parent_creation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target-parent creation error is reported before an output write."""
    skill_dir = _make_skill_dir(tmp_path, "# Content")
    target_file = tmp_path / "out" / "my-skill.md"
    original_mkdir = Path.mkdir

    def _raise_on_target_parent(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == target_file.parent:
            raise OSError("mkdir denied")
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _raise_on_target_parent)

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 1
    assert result.rendered == 0
    assert target_file.exists() is False


# ---------------------------------------------------------------------------
# 6. Write permission denied → errors == 1, no exception propagates
# ---------------------------------------------------------------------------


def test_install_skill_flat_write_permission_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install_skill_flat handles OSError on write gracefully (errors=1, no raise).

    Uses monkeypatch to inject an OSError on Path.write_text so the test is
    reliable regardless of whether the process runs as root.
    """
    skill_dir = _make_skill_dir(tmp_path, "# Content")
    target_file = tmp_path / "out" / "my-skill.md"

    original_write_text = Path.write_text

    def _raise_on_target(self: Path, *args: object, **kwargs: object) -> int:
        if self == target_file:
            raise OSError("Permission denied")
        # Allow other write_text calls through; Path.write_text returns int
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _raise_on_target)

    # Must not raise — errors are counted, not propagated
    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 1
    assert result.rendered == 0


# ---------------------------------------------------------------------------
# 7. Empty ctx: {{key}} tokens preserved verbatim
# ---------------------------------------------------------------------------


def test_install_skill_flat_empty_ctx(tmp_path: Path) -> None:
    """With an empty ctx, {{key}} tokens are preserved verbatim in the output."""
    skill_dir = _make_skill_dir(tmp_path, "Hello {{name}}, your role is {{role}}.")
    target_file = tmp_path / "out" / "my-skill.md"

    result = install_skill_flat(skill_dir, target_file, {})

    assert result.errors == 0
    assert result.rendered == 1
    content = target_file.read_text(encoding="utf-8")
    # Tokens must be preserved exactly as-is
    assert "{{name}}" in content
    assert "{{role}}" in content
    # Unresolved tokens are warned but NOT substituted
    assert result.warned == 2
