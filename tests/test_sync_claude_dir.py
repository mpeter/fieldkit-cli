"""Unit tests for scripts/sync_claude_dir.py.

Focus: the _CLAUDE_EXCLUDE carve-out. .claude/ mirrors .opencode/{agents,commands},
but the proctor review pipeline is OpenCode-only — its commands dispatch to the
proctor-* agent variants declared in .opencode/opencode.jsonc, which Claude Code
cannot read. Mirroring them would ship commands that spawn agents that do not
resolve.

Three properties:
  1. excluded names are never copied into .claude/
  2. a stray .claude/ copy of an excluded name is pruned as an orphan
  3. non-excluded files still mirror normally (guards against over-broad exclusion)
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_claude_dir as scd  # noqa: E402


def test_public_tree_without_private_agent_adapters_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scd, "REPO_ROOT", tmp_path)

    exit_code = scd.main(["--check"])

    assert exit_code == 0
    assert "not part of this repository" in capsys.readouterr().out


def _mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point the script's _PAIRS at a throwaway source/dest pair under tmp_path.

    REPO_ROOT is repointed too: the script renders progress lines via
    dst.relative_to(REPO_ROOT), which raises for paths outside the real repo.
    """
    src_dir = tmp_path / ".opencode" / "commands"
    dst_dir = tmp_path / ".claude" / "commands"
    src_dir.mkdir(parents=True)
    dst_dir.mkdir(parents=True)
    skills_dir = tmp_path / ".opencode" / "skills"
    skills_dir.mkdir()
    (tmp_path / ".claude" / "skills").symlink_to(Path("../.opencode/skills"), target_is_directory=True)
    monkeypatch.setattr(scd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scd, "_PAIRS", [(src_dir, dst_dir)])
    return src_dir, dst_dir


@pytest.mark.parametrize("mode", ["missing", "directory", "wrong-target"])
@pytest.mark.parametrize("arguments", [[], ["--check"]])
def test_invalid_skills_bridge_fails_without_modifying_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    arguments: list[str],
) -> None:
    """Invalid bridge states fail in both modes and preserve whatever is present."""
    link = tmp_path / ".claude" / "skills"
    link.parent.mkdir(parents=True)
    if mode == "directory":
        link.mkdir()
        (link / "local.md").write_text("preserve me\n")
    elif mode == "wrong-target":
        link.symlink_to(Path("../other/skills"), target_is_directory=True)
    monkeypatch.setattr(scd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scd, "_PAIRS", [])

    exit_code = scd.main(arguments)

    assert exit_code == 1
    assert ".claude/skills must be a symlink to ../.opencode/skills" in capsys.readouterr().err
    if mode == "missing":
        assert not link.exists()
    elif mode == "directory":
        assert (link / "local.md").read_text() == "preserve me\n"
    else:
        assert link.is_symlink()
        assert link.readlink() == Path("../other/skills")


def test_canonical_skills_bridge_allows_mirror_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The portable relative bridge permits ordinary mirror validation."""
    _mirror(tmp_path, monkeypatch)

    exit_code = scd.main(["--check"])

    assert exit_code == 0


@pytest.mark.parametrize("excluded", ["proctor.md", "review-pr.md", "review-council.md"])
def test_excluded_names_are_never_copied_into_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excluded: str
) -> None:
    """An .opencode/ file whose name is excluded must not be mirrored into .claude/."""
    src_dir, dst_dir = _mirror(tmp_path, monkeypatch)
    (src_dir / excluded).write_text("proctor pipeline body\n")

    exit_code = scd.main([])

    assert exit_code == 0
    assert not (dst_dir / excluded).exists()


def test_stray_excluded_copy_in_claude_is_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .claude/ copy of an excluded name has no valid source and must be pruned."""
    src_dir, dst_dir = _mirror(tmp_path, monkeypatch)
    (src_dir / "proctor.md").write_text("proctor pipeline body\n")
    stray = dst_dir / "proctor.md"
    stray.write_text("stale mirrored copy\n")

    exit_code = scd.main(["--prune"])

    assert exit_code == 1
    assert not stray.exists()


def test_non_excluded_files_still_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary commands must still sync — the exclusion must not over-reach."""
    src_dir, dst_dir = _mirror(tmp_path, monkeypatch)
    (src_dir / "foreman.md").write_text("foreman body\n")

    exit_code = scd.main([])

    assert exit_code == 1
    assert (dst_dir / "foreman.md").read_text() == "foreman body\n"


def test_check_mode_reports_excluded_stray_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check must report an excluded stray as an orphan but leave it on disk."""
    src_dir, dst_dir = _mirror(tmp_path, monkeypatch)
    (src_dir / "proctor.md").write_text("proctor pipeline body\n")
    stray = dst_dir / "proctor.md"
    stray.write_text("stale mirrored copy\n")

    exit_code = scd.main(["--check"])

    assert exit_code == 1
    assert stray.exists()
    assert "ORPHAN" in capsys.readouterr().out
