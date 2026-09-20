"""Tests for _runner.run(), _skills_dir(), and _cmd_show() (Track B CRAP cleanup).

Targets (this file only):
- run          — cc=5,  CI coverage 0%
- _skills_dir  — cc=11, CI coverage 47%
- _cmd_show    — cc=12, CI coverage 63%
"""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.commands.skill._runner as runner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_skills_dir_cache() -> Iterator[None]:
    """_skills_dir is @functools.cache'd; a stale value would leak into other test files."""
    runner._skills_dir.cache_clear()
    yield
    runner._skills_dir.cache_clear()


# ---------------------------------------------------------------------------
# run() — subcommand dispatch (complexity 5, CI coverage 0%)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcmd", "target"),
    [
        ("list", "_cmd_list"),
        ("show", "_cmd_show"),
        ("variables", "_cmd_variables"),
    ],
)
def test_run_dispatches_to_matching_subcommand_with_argv(subcmd: str, target: str) -> None:
    """Each recognised subcmd routes to its own handler, called with the raw argv."""
    argv = ["--some-flag", "value"]
    with patch.object(runner, target, return_value=42) as mock_fn:
        rc = runner.run(subcmd, argv)
    assert rc == 42
    mock_fn.assert_called_once_with(argv)


def test_run_install_ignores_argv_and_calls_with_no_preselections() -> None:
    """run('install', argv) always calls _cmd_install([], []), never argv itself.

    The dispatcher's docstring documents this path as non-interactive with no
    pre-selections; silently forwarding argv would change that contract without
    anyone noticing (flags would appear to be accepted but do nothing).
    """
    argv = ["--tool", "claude", "--skill", "xlsx"]
    with patch.object(runner, "_cmd_install", return_value=7) as mock_fn:
        rc = runner.run("install", argv)
    assert rc == 7
    mock_fn.assert_called_once_with([], [])


def test_run_unknown_subcommand_prints_error_and_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = runner.run("bogus-subcmd", [])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Unknown subcommand: 'bogus-subcmd'" in captured.err


# ---------------------------------------------------------------------------
# _skills_dir() — resolution priority order (complexity 11, CI coverage 47%)
# ---------------------------------------------------------------------------


def _write_config(home: Path, fieldkit_root: Path) -> None:
    config_dir = home / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(f"fieldkit_root: {fieldkit_root}\n", encoding="utf-8")


def test_skills_dir_env_override_expands_tilde_and_resolves_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priority 1: FIELDKIT_SKILLS_DIR wins, and is both expanduser()'d and resolve()'d."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FIELDKIT_SKILLS_DIR", "~/skills-here/nested/..")

    result = runner._skills_dir()

    assert result == tmp_path / "skills-here"


def test_skills_dir_config_root_prefers_agents_skills_over_bare_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priority 2: when both `.agents/skills` and `skills` exist, `.agents/skills` wins."""
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runner, "CONFIG_PATH", home / ".config" / "fieldkit" / "config.yaml")
    root = tmp_path / "project-root"
    agents_skills = root / ".agents" / "skills"
    agents_skills.mkdir(parents=True)
    (root / "skills").mkdir(parents=True)
    _write_config(home, root)

    result = runner._skills_dir()

    assert result == agents_skills


def test_skills_dir_config_root_falls_back_to_bare_skills_when_agents_dir_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priority 2 fallback: `skills` is used when `.agents/skills` doesn't exist."""
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runner, "CONFIG_PATH", home / ".config" / "fieldkit" / "config.yaml")
    root = tmp_path / "project-root"
    bare_skills = root / "skills"
    bare_skills.mkdir(parents=True)
    _write_config(home, root)

    result = runner._skills_dir()

    assert result == bare_skills


def test_skills_dir_malformed_config_yaml_falls_through_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken config.yaml must not blow up _skills_dir — it falls through to later steps."""
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runner, "CONFIG_PATH", home / ".config" / "fieldkit" / "config.yaml")
    config_dir = home / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("fieldkit_root: [unterminated\n", encoding="utf-8")

    result = runner._skills_dir()

    assert isinstance(result, Path)
    assert result.is_dir()


def test_skills_dir_importlib_resources_failure_falls_back_to_repo_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priority 4 (last resort): when importlib.resources can't find the package,
    fall back to the fixed skill/->commands/->fieldkit/->src/->repo-root/skills path.
    """
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # no ~/.config/fieldkit/config.yaml here
    monkeypatch.setattr(runner, "CONFIG_PATH", home / ".config" / "fieldkit" / "config.yaml")

    expected = Path(runner.__file__).resolve().parent.parent.parent.parent.parent / "skills"

    with patch("importlib.resources.files", side_effect=ModuleNotFoundError("no such package")):
        result = runner._skills_dir()

    assert result == expected


# ---------------------------------------------------------------------------
# _cmd_show() — skill show subcommand (complexity 12, CI coverage 63%)
# ---------------------------------------------------------------------------


def _write_skill_md(skill_dir: Path, name: str, description: str = "A skill.") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


class _OrderedDirStub:
    """Path-like stub yielding iterdir() entries in a caller-specified order.

    Path.iterdir() readdir ordering is not guaranteed by pathlib/POSIX, so a
    real-filesystem test of candidate selection could pass or fail depending
    on directory entry order rather than the code under test. This stub makes
    that order explicit and deterministic.
    """

    def __init__(self, base: Path, order: list[str]) -> None:
        self._base = base
        self._order = order

    def is_dir(self) -> bool:
        return self._base.is_dir()

    def iterdir(self):
        return (self._base / name for name in self._order)

    def __str__(self) -> str:
        return str(self._base)


def test_cmd_show_no_names_prints_usage_and_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = runner._cmd_show([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Usage: fieldkit skill show <skill-name> [--json]" in captured.err


def test_cmd_show_missing_skills_dir_prints_both_hints_and_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_dir = tmp_path / "does-not-exist"
    config_path = tmp_path / "xdg-config" / "fieldkit" / "config.yaml"
    with (
        patch.object(runner, "_skills_dir", return_value=missing_dir),
        patch.object(runner, "CONFIG_PATH", config_path),
    ):
        rc = runner._cmd_show(["anything"])

    assert rc == 1
    captured = capsys.readouterr()
    assert f"Skills directory not found: {missing_dir}" in captured.err
    assert "FIELDKIT_SKILLS_DIR" in captured.err
    assert str(config_path) in captured.err


def test_cmd_show_multiple_candidates_prefers_exact_match_over_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exact-name match must win even when it is not first in directory order."""
    _write_skill_md(tmp_path / "foo", "foo")
    _write_skill_md(tmp_path / "foo-extra", "foo-extra")
    # "foo-extra" is listed before the exact match "foo" on purpose.
    stub = _OrderedDirStub(tmp_path, ["foo-extra", "foo"])

    with patch.object(runner, "_skills_dir", return_value=stub):
        rc = runner._cmd_show(["foo", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "foo"


def test_cmd_show_skill_load_failure_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken-skill"
    broken.mkdir()  # no SKILL.md — _load_skill returns None

    with patch.object(runner, "_skills_dir", return_value=tmp_path):
        rc = runner._cmd_show(["broken-skill"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "Could not load skill 'broken-skill'." in captured.err


def test_cmd_show_no_candidates_prints_not_found_and_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(runner, "_skills_dir", return_value=tmp_path):
        rc = runner._cmd_show(["nonexistent-skill"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "Skill 'nonexistent-skill' not found." in captured.err
    assert "fieldkit skill list" in captured.err


def test_cmd_show_without_json_flag_renders_human_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_skill_md(tmp_path / "myskill", "myskill", description="A test skill.")

    with (
        patch.object(runner, "_skills_dir", return_value=tmp_path),
        patch.object(runner, "_render_show_human") as mock_render,
    ):
        rc = runner._cmd_show(["myskill"])

    assert rc == 0
    mock_render.assert_called_once()
    assert capsys.readouterr().out == ""


def test_cmd_show_json_flag_prints_json_and_skips_human_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_skill_md(tmp_path / "myskill", "myskill", description="A test skill.")

    with (
        patch.object(runner, "_skills_dir", return_value=tmp_path),
        patch.object(runner, "_render_show_human") as mock_render,
    ):
        rc = runner._cmd_show(["myskill", "--json"])

    assert rc == 0
    mock_render.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "myskill"
    assert payload["description"] == "A test skill."
