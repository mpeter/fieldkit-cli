"""Contract tests for safe skill overwrite and unchanged detection (historic regression)."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import fieldkit.commands.skill._install_execution as install_execution
import fieldkit.commands.skill._runner as runner
from fieldkit.__main__ import cli as fieldkit_cli
from fieldkit.commands.skill._runner import _cmd_install
from fieldkit.skill.install import installed_skill_digest, rendered_skill_digest
from fieldkit.skill.template import install_skill_flat

pytestmark = pytest.mark.unit


@pytest.fixture()
def skill_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "bundled"
    for name in ("alpha", "beta"):
        skill_dir = source / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        (skill_dir / "notes.md").write_text(f"notes for {name}\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_skills_dir", lambda: source)
    monkeypatch.setattr("fieldkit.skill.template.build_template_ctx", lambda: {})
    return source


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    return project


def _manifest(project: Path) -> Path:
    return project / ".opencode" / "skills" / ".fieldkit-install-manifest.json"


def test_directory_and_flat_digests_match_rendered_content(tmp_path: Path, skill_source: Path) -> None:
    directory_target = tmp_path / "alpha" / "SKILL.md"
    directory_target.parent.mkdir()
    for source in (skill_source / "alpha").iterdir():
        (directory_target.parent / source.name).write_bytes(source.read_bytes())
    flat_target = tmp_path / "alpha.md"
    assert install_skill_flat(skill_source / "alpha", flat_target, {}).errors == 0

    directory_digest = installed_skill_digest(directory_target, "directory")
    flat_digest = installed_skill_digest(flat_target, "flat")

    assert directory_digest == rendered_skill_digest(skill_source / "alpha", "directory", {})
    assert flat_digest == rendered_skill_digest(skill_source / "alpha", "flat", {})


def test_supporting_file_edit_blocks_directory_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    notes = project / ".opencode" / "skills" / "alpha" / "notes.md"
    notes.write_text("local judgment\n", encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha"])

    assert result == 1
    assert notes.read_text(encoding="utf-8") == "local judgment\n"


def test_force_overwrites_drift_and_refreshes_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed = project / ".opencode" / "skills" / "alpha" / "SKILL.md"
    installed.write_text("local\n", encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha"], force=True)

    manifest = json.loads(_manifest(project).read_text(encoding="utf-8"))
    assert result == 0
    assert installed.read_text(encoding="utf-8") == "# alpha\n"
    assert manifest["digests"]["alpha"] == installed_skill_digest(installed, "directory")


def test_legacy_manifest_drift_requires_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    installed = project / ".opencode" / "skills" / "alpha"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("legacy local edit\n", encoding="utf-8")
    _manifest(project).write_text('{"version": 1, "skills": ["alpha"]}', encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha"])

    assert result == 1
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "legacy local edit\n"


def test_untrusted_directory_without_skill_file_is_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    installed = project / ".opencode" / "skills" / "alpha"
    installed.mkdir(parents=True)
    local_notes = installed / "notes.md"
    local_notes.write_text("local judgment\n", encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha"])

    assert result == 1
    assert local_notes.read_text(encoding="utf-8") == "local judgment\n"
    assert not (installed / "SKILL.md").exists()


def test_divergent_later_skill_blocks_entire_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha", "beta"]) == 0
    alpha = project / ".opencode" / "skills" / "alpha" / "SKILL.md"
    beta = project / ".opencode" / "skills" / "beta" / "SKILL.md"
    (skill_source / "alpha" / "SKILL.md").write_text("# upstream alpha\n", encoding="utf-8")
    beta.write_text("local beta\n", encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha", "beta"])

    assert result == 1
    assert alpha.read_text(encoding="utf-8") == "# alpha\n"
    assert beta.read_text(encoding="utf-8") == "local beta\n"


def test_target_change_after_preflight_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed = project / ".opencode" / "skills" / "alpha" / "SKILL.md"
    original_install = install_execution._install_to_tools

    def edit_before_install(*args: Any, **kwargs: Any) -> tuple[int, int, int, int]:
        installed.write_text("concurrent local edit\n", encoding="utf-8")
        return original_install(*args, **kwargs)

    monkeypatch.setattr(install_execution, "_install_to_tools", edit_before_install)

    result = _cmd_install(["opencode"], ["alpha"])

    assert result == 1
    assert installed.read_text(encoding="utf-8") == "concurrent local edit\n"


def test_bundled_file_removal_is_applied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed_notes = project / ".opencode" / "skills" / "alpha" / "notes.md"
    (skill_source / "alpha" / "notes.md").unlink()

    assert _cmd_install(["opencode"], ["alpha"]) == 0
    assert not installed_notes.exists()
    capsys.readouterr()

    assert _cmd_install(["opencode"], ["alpha"]) == 0
    assert "→ unchanged" in capsys.readouterr().out


def test_unchanged_install_preserves_mtime_and_upgrades_legacy_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed = project / ".opencode" / "skills" / "alpha" / "SKILL.md"
    before = installed.stat().st_mtime_ns
    _manifest(project).write_text('{"version": 1, "skills": ["alpha"]}', encoding="utf-8")

    result = _cmd_install(["opencode"], ["alpha"])

    manifest = json.loads(_manifest(project).read_text(encoding="utf-8"))
    assert result == 0
    assert installed.stat().st_mtime_ns == before
    assert manifest["version"] == 2
    assert "alpha" in manifest["digests"]


def test_dry_run_force_is_non_mutating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path) -> None:
    project = _project(tmp_path, monkeypatch)
    target = project / ".opencode" / "skills" / "alpha" / "SKILL.md"

    result = _cmd_install(["opencode"], ["alpha"], dry_run=True, force=True)

    assert result == 0
    assert not target.exists()
    assert not _manifest(project).exists()


def test_click_cli_plumbs_force_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = _project(tmp_path, monkeypatch)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed = project / ".opencode" / "skills" / "alpha" / "SKILL.md"
    installed.write_text("local\n", encoding="utf-8")

    result = CliRunner().invoke(
        fieldkit_cli,
        ["skill", "install", "--tool", "opencode", "--skill", "alpha", "--force"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert installed.read_text(encoding="utf-8") == "# alpha\n"


def test_force_does_not_bypass_target_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".opencode").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(project)

    result = _cmd_install(["opencode"], ["alpha"], force=True)

    assert result == 1
    assert not (outside / "skills" / "alpha" / "SKILL.md").exists()
