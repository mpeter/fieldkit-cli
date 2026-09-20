"""Contract tests for bounded global skill installation (implementation change)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import fieldkit.commands.skill._runner as runner
from fieldkit.__main__ import cli as fieldkit_cli
from fieldkit.commands.skill._runner import _cmd_install
from fieldkit.commands.skill._target_resolution import validate_global_install_request

pytestmark = pytest.mark.unit


@pytest.fixture()
def global_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Provide an isolated home and bundled continuity skills."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "bundled"
    for name in ("handoffs", "pickup"):
        skill_dir = source / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\nHello {{{{name}}}}!\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(runner, "_skills_dir", lambda: source)
    monkeypatch.setattr("fieldkit.skill.template.build_template_ctx", lambda: {"name": "Operator"})
    return home, source


def test_global_opencode_writes_rendered_skill_and_manifest(global_env: tuple[Path, Path]) -> None:
    home, _ = global_env

    result = _cmd_install(["opencode"], ["handoffs"], global_install=True)

    installed = home / ".agents" / "skills" / "handoffs" / "SKILL.md"
    manifest = home / ".agents" / "skills" / ".fieldkit-install-manifest.json"
    assert result == 0
    assert installed.read_text(encoding="utf-8") == "# handoffs\nHello Operator!\n"
    assert json.loads(manifest.read_text(encoding="utf-8"))["skills"] == ["handoffs"]


def test_global_claude_writes_registered_root(global_env: tuple[Path, Path]) -> None:
    home, _ = global_env

    result = _cmd_install(["claude-code"], ["pickup"], global_install=True)

    assert result == 0
    assert (home / ".claude" / "skills" / "pickup" / "SKILL.md").is_file()


def test_click_cli_plumbs_global_install(global_env: tuple[Path, Path]) -> None:
    home, _ = global_env

    result = CliRunner().invoke(
        fieldkit_cli,
        ["skill", "install", "--global", "--tool", "opencode", "--skill", "handoffs"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert (home / ".agents" / "skills" / "handoffs" / "SKILL.md").is_file()


def test_global_install_never_detects_tools_from_cwd(
    global_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fieldkit.commands.skill._target_resolution.detect_tools", lambda _cwd: pytest.fail("CWD detection ran")
    )

    result = _cmd_install(["opencode"], ["handoffs"], global_install=True)

    assert result == 0


def test_global_aliased_roots_are_installed_once(
    global_env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    home, _ = global_env
    agents_skills = home / ".agents" / "skills"
    agents_skills.mkdir(parents=True)
    claude = home / ".claude"
    claude.mkdir()
    (claude / "skills").symlink_to(agents_skills, target_is_directory=True)

    result = _cmd_install(["opencode", "claude-code"], ["handoffs"], global_install=True)

    assert result == 0
    assert (agents_skills / "handoffs" / "SKILL.md").is_file()
    assert "1 tool(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("tools", "skills", "install_all", "prune", "message"),
    [
        ([], ["handoffs"], False, False, "explicit --tool"),
        (["cursor"], ["handoffs"], False, False, "does not support tool"),
        (["opencode"], [], False, False, "requires --skill"),
        (["opencode"], ["brief"], False, False, "does not support skill"),
        (["opencode"], [], True, False, "does not support --all"),
    ],
)
def test_invalid_global_selection_is_rejected(
    global_env: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    tools: list[str],
    skills: list[str],
    install_all: bool,
    prune: bool,
    message: str,
) -> None:
    home, _ = global_env

    result = _cmd_install(tools, skills, install_all=install_all, global_install=True, prune=prune)

    assert result == 1
    assert message in capsys.readouterr().err
    assert not (home / ".agents" / "skills").exists()


@pytest.mark.parametrize(
    ("tools", "skills", "install_all", "prune", "expected"),
    [
        (["opencode"], ["handoffs"], False, False, None),
        (["claude-code"], [], False, True, None),
        ([], ["handoffs"], False, False, "explicit --tool"),
        (["gemini"], ["handoffs"], False, False, "does not support tool"),
        (["opencode"], ["handoffs"], True, False, "does not support --all"),
        (["opencode"], [], False, False, "requires --skill"),
        (["opencode"], ["brief"], False, False, "does not support skill"),
    ],
)
def test_validate_global_install_request_contract(
    tools: list[str], skills: list[str], install_all: bool, prune: bool, expected: str | None
) -> None:
    result = validate_global_install_request(tools, skills, install_all=install_all, prune=prune)

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert expected in result


@pytest.mark.parametrize("parent_name", [".agents", ".claude"])
def test_symlinked_global_parent_is_rejected(
    global_env: tuple[Path, Path],
    tmp_path: Path,
    parent_name: str,
) -> None:
    home, _ = global_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / parent_name).symlink_to(outside, target_is_directory=True)
    tool = "opencode" if parent_name == ".agents" else "claude-code"

    result = _cmd_install([tool], ["handoffs"], global_install=True)

    assert result == 1
    assert not (outside / "skills" / "handoffs").exists()


def test_global_root_symlink_escape_is_rejected(global_env: tuple[Path, Path], tmp_path: Path) -> None:
    home, _ = global_env
    agents = home / ".agents"
    agents.mkdir()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (agents / "skills").symlink_to(outside, target_is_directory=True)

    result = _cmd_install(["opencode"], ["handoffs"], global_install=True)

    assert result == 1
    assert not (outside / "handoffs").exists()


@pytest.mark.parametrize(("tool", "parent_name"), [("opencode", ".agents"), ("claude-code", ".claude")])
def test_global_root_symlink_loop_returns_validation_error(
    global_env: tuple[Path, Path], tool: str, parent_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    home, _ = global_env
    parent = home / parent_name
    parent.mkdir()
    (parent / "skills").symlink_to("skills", target_is_directory=True)

    result = _cmd_install([tool], ["handoffs"], global_install=True)

    assert result == 1
    assert "Could not validate registered skill root" in capsys.readouterr().err


def test_global_candidate_symlink_escape_is_rejected_even_with_force(
    global_env: tuple[Path, Path], tmp_path: Path
) -> None:
    home, _ = global_env
    skills_root = home / ".agents" / "skills"
    skills_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (skills_root / "handoffs").symlink_to(outside, target_is_directory=True)

    result = _cmd_install(["opencode"], ["handoffs"], global_install=True, force=True)

    assert result == 1
    assert not (outside / "SKILL.md").exists()


def test_global_drift_requires_force(global_env: tuple[Path, Path]) -> None:
    home, _ = global_env
    assert _cmd_install(["opencode"], ["handoffs"], global_install=True) == 0
    installed = home / ".agents" / "skills" / "handoffs" / "SKILL.md"
    installed.write_text("local edit\n", encoding="utf-8")

    refused = _cmd_install(["opencode"], ["handoffs"], global_install=True)
    forced = _cmd_install(["opencode"], ["handoffs"], global_install=True, force=True)

    assert refused == 1
    assert forced == 0
    assert installed.read_text(encoding="utf-8") == "# handoffs\nHello Operator!\n"


def test_global_prune_only_allows_explicit_tool(
    global_env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    result = _cmd_install(["opencode"], [], global_install=True, prune=True, dry_run=True)

    assert result == 0
    assert "no stale installer-owned skills" in capsys.readouterr().out


def test_global_dry_run_requires_skill_for_install(global_env: tuple[Path, Path]) -> None:
    result = _cmd_install(["opencode"], [], global_install=True, dry_run=True)

    assert result == 1


@pytest.mark.parametrize(("tool", "marker"), [("opencode", ".opencode"), ("claude-code", ".claude")])
def test_project_install_rejects_explicit_home_harness(
    global_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tool: str, marker: str
) -> None:
    home, _ = global_env
    (home / marker).mkdir()
    monkeypatch.chdir(home)

    result = _cmd_install([tool], ["handoffs"])

    assert result == 1


def test_project_detection_ignores_home_harnesses(
    global_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = global_env
    (home / ".opencode").mkdir()
    (home / ".claude").mkdir()
    monkeypatch.chdir(home)
    monkeypatch.setattr(runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    result = _cmd_install([], [])

    assert result == 0
    assert not (home / ".opencode" / "skills").exists()
    assert not (home / ".claude" / "skills").exists()
