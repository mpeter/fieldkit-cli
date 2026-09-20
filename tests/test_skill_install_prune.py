"""Contract tests for installer-owned skill pruning (implementation change)."""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

import fieldkit.commands.skill._runner as runner
from fieldkit.__main__ import cli as fieldkit_cli
from fieldkit.commands.skill._runner import _cmd_install

pytestmark = pytest.mark.unit


@pytest.fixture()
def skill_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "bundled"
    for name in ("alpha", "beta"):
        skill_dir = source / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_skills_dir", lambda: source)
    monkeypatch.setattr("fieldkit.skill.template.build_template_ctx", lambda: {})
    return source


def _manifest(project: Path, tool: str = "opencode") -> Path:
    if tool == "cursor":
        return project / ".cursor" / "rules" / ".fieldkit-install-manifest.json"
    return project / ".opencode" / "skills" / ".fieldkit-install-manifest.json"


def test_successful_installs_accumulate_manifest_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)

    assert _cmd_install(["opencode"], ["alpha"]) == 0
    assert _cmd_install(["opencode"], ["beta"]) == 0

    manifest = _manifest(project)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["skills"] == ["alpha", "beta"]
    assert data["version"] == 2
    assert set(data["digests"]) == {"alpha", "beta"}


def test_dry_run_creates_no_manifest_or_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)

    assert _cmd_install(["opencode"], ["alpha"], dry_run=True) == 0

    manifest = _manifest(project)
    assert not manifest.exists()
    assert not manifest.with_suffix(".json.lock").exists()


def test_prune_preview_retains_owned_stale_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = _cmd_install(["opencode"], [], prune=True)

    assert result == 0
    assert (project / ".opencode" / "skills" / "alpha" / "SKILL.md").exists()
    assert "alpha" in json.loads(_manifest(project).read_text(encoding="utf-8"))["skills"]
    assert "preview" in capsys.readouterr().out.lower()


def test_confirmed_prune_removes_owned_directory_and_preserves_local_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    local = project / ".opencode" / "skills" / "local-only"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("# local\n", encoding="utf-8")
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = _cmd_install(["opencode"], [], prune=True, confirm=True)

    assert result == 0
    assert not (project / ".opencode" / "skills" / "alpha").exists()
    assert local.exists()
    assert json.loads(_manifest(project).read_text(encoding="utf-8"))["skills"] == []
    assert "1 untracked" in capsys.readouterr().out


def test_confirmed_prune_removes_owned_flat_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["cursor"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = _cmd_install(["cursor"], [], prune=True, confirm=True)

    assert result == 0
    assert not (project / ".cursor" / "rules" / "alpha.md").exists()
    assert json.loads(_manifest(project, "cursor").read_text(encoding="utf-8"))["skills"] == []


def test_invalid_manifest_name_blocks_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    manifest = _manifest(project)
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"version": 1, "skills": ["../../outside"]}', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    monkeypatch.chdir(project)

    result = _cmd_install(["opencode"], [], prune=True, confirm=True)

    assert result == 1
    assert outside.read_text(encoding="utf-8") == "keep"
    assert "invalid" in capsys.readouterr().err.lower()


def test_install_failure_suppresses_confirmed_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()
    (skill_source / "broken").mkdir()

    result = _cmd_install(["opencode"], ["broken"], prune=True, confirm=True)

    assert result == 1
    assert (project / ".opencode" / "skills" / "alpha").exists()
    assert json.loads(_manifest(project).read_text(encoding="utf-8"))["skills"] == ["alpha"]
    assert "pruning skipped" in capsys.readouterr().err.lower()


def test_confirmed_prune_rejects_owned_symlink_outside_skill_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    installed = project / ".opencode" / "skills" / "alpha"
    for child in installed.iterdir():
        child.unlink()
    installed.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    installed.symlink_to(outside, target_is_directory=True)
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = _cmd_install(["opencode"], [], prune=True, confirm=True)

    assert result == 1
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert installed.is_symlink()


def test_dry_run_dominates_confirmed_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = _cmd_install(["opencode"], [], dry_run=True, prune=True, confirm=True)

    assert result == 0
    assert (project / ".opencode" / "skills" / "alpha" / "SKILL.md").exists()
    assert json.loads(_manifest(project).read_text(encoding="utf-8"))["skills"] == ["alpha"]


def test_confirm_without_prune_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)

    assert _cmd_install(["opencode"], ["alpha"], confirm=True) == 1
    assert not (project / ".opencode" / "skills" / "alpha").exists()


def test_click_cli_plumbs_confirmed_prune_to_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    result = CliRunner().invoke(
        fieldkit_cli,
        ["skill", "install", "--tool", "opencode", "--prune", "--confirm"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert not (project / ".opencode" / "skills" / "alpha").exists()


@pytest.mark.parametrize(("confirm", "expected_status"), [(False, "pending"), (True, "completed")])
def test_click_cli_json_reports_prune_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_source: Path,
    confirm: bool,
    expected_status: str,
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()

    args = ["skill", "install", "--tool", "opencode", "--prune", "--json"]
    if confirm:
        args.append("--confirm")
    result = CliRunner().invoke(fieldkit_cli, args, catch_exceptions=False)

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["outcomes"][expected_status] == [{"skill": "alpha", "tool": "opencode"}]
    assert (project / ".opencode" / "skills" / "alpha").exists() is (not confirm)


def test_click_cli_json_reports_partial_prune_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_source: Path
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha", "beta"]) == 0
    for skill_name in ("alpha", "beta"):
        for child in (skill_source / skill_name).iterdir():
            child.unlink()
        (skill_source / skill_name).rmdir()

    real_rmtree = shutil.rmtree

    def fail_on_beta(path: Path) -> None:
        if path.name == "beta":
            raise PermissionError("read-only target")
        real_rmtree(path)

    with monkeypatch.context() as scoped:
        scoped.setattr("fieldkit.skill.install.shutil.rmtree", fail_on_beta)
        result = CliRunner().invoke(
            fieldkit_cli,
            ["skill", "install", "--tool", "opencode", "--prune", "--confirm", "--json"],
            catch_exceptions=False,
        )

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["outcomes"]["completed"] == [{"skill": "alpha", "tool": "opencode"}]
    assert payload["outcomes"]["failed"] == [{"skill": "beta", "tool": "opencode"}]
    assert not (project / ".opencode" / "skills" / "alpha").exists()
    assert (project / ".opencode" / "skills" / "beta").exists()


def test_confirmed_prune_rejects_missing_bundled_skill_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert _cmd_install(["opencode"], ["alpha"]) == 0
    missing_source = tmp_path / "missing-bundle"
    monkeypatch.setattr(runner, "_skills_dir", lambda: missing_source)

    result = _cmd_install(["opencode"], [], prune=True, confirm=True)

    assert result == 1
    assert (project / ".opencode" / "skills" / "alpha" / "SKILL.md").exists()
    assert "bundled skill directory is missing" in capsys.readouterr().err


def test_confirmed_prune_rejects_symlinked_tool_root_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skill_source: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external_root = tmp_path / "external-skills"
    owned = external_root / "alpha"
    owned.mkdir(parents=True)
    (owned / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    manifest = external_root / ".fieldkit-install-manifest.json"
    manifest.write_text('{"version": 1, "skills": ["alpha"]}', encoding="utf-8")
    opencode = project / ".opencode"
    opencode.mkdir()
    (opencode / "skills").symlink_to(external_root, target_is_directory=True)
    for child in (skill_source / "alpha").iterdir():
        child.unlink()
    (skill_source / "alpha").rmdir()
    monkeypatch.chdir(project)

    result = _cmd_install(["opencode"], [], prune=True, confirm=True)

    assert result == 1
    assert (owned / "SKILL.md").exists()
    assert "escapes project directory" in capsys.readouterr().err
