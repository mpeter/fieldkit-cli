"""Tests for backward-compatible config key aliases (historic regression).

Covers:
- data_repo key silently aliased to fieldkit_home
- data_repo deprecation WARNING logged
- fieldkit_home takes precedence when both keys present
- Neither key raises ConfigError with correct message
"""

from pathlib import Path

import pytest

import fieldkit.config._loader
from fieldkit.config import ConfigError, get_fieldkit_home

pytestmark = pytest.mark.unit


# ── TestDataRepoAlias (flattened) ───────────────────────────────────────────


def test_data_repo_alias_data_repo_returns_correct_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("data_repo: /tmp/my-fieldkit\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_fieldkit_home()

    assert result == Path("/tmp/my-fieldkit")
    assert result.is_absolute(), "get_fieldkit_home must return an absolute Path"


def test_data_repo_alias_data_repo_emits_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 043 G3b: data_repo fallback emits DeprecationWarning via warnings.warn."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("data_repo: /tmp/my-fieldkit\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.warns(DeprecationWarning, match=r"data_repo"):
        get_fieldkit_home()


def test_data_repo_alias_data_repo_tilde_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("data_repo: ~/fieldkit-workspace\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_fieldkit_home()

    assert result == Path("~/fieldkit-workspace").expanduser().resolve()
    assert result.is_absolute()


def test_data_repo_alias_fieldkit_home_takes_precedence_over_data_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fieldkit_home wins when both keys are present (shouldn't normally happen)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "fieldkit_home: /tmp/correct-home\ndata_repo: /tmp/stale-repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    result = get_fieldkit_home()

    assert result == Path("/tmp/correct-home")


def test_data_repo_alias_neither_key_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("some_other_key: value\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.raises(ConfigError, match="fieldkit_home"):
        get_fieldkit_home()


def test_data_repo_alias_data_repo_empty_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("data_repo: ''\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    with pytest.raises(ConfigError, match="data_repo"):
        get_fieldkit_home()


# ---------------------------------------------------------------------------
# implementation change: get_shadowbot_assistant_id
# ---------------------------------------------------------------------------


# ── TestGetShadowbotAssistantId (flattened) ─────────────────────────────────


def test_get_shadowbot_assistant_id_returns_default_when_no_shadowbot_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fk\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_shadowbot_assistant_id

    assert get_shadowbot_assistant_id() == "sales_assistant_v2"


def test_get_shadowbot_assistant_id_returns_configured_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "fieldkit_home: /tmp/fk\nshadowbot:\n  assistant_id: sales_assistant_v3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_shadowbot_assistant_id

    assert get_shadowbot_assistant_id() == "sales_assistant_v3"


def test_get_shadowbot_assistant_id_returns_default_when_assistant_id_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "fieldkit_home: /tmp/fk\nshadowbot:\n  assistant_id: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_shadowbot_assistant_id

    assert get_shadowbot_assistant_id() == "sales_assistant_v2"


# ── get_github_repo ConfigError cases ─────────────────────────────────────────


def test_get_github_repo_returns_trimmed_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured repository slug is returned without surrounding whitespace."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("github_repo: '  example/fieldkit-project  '\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_github_repo

    result = get_github_repo()

    assert result == "example/fieldkit-project"


# ── TestGetIssuesDirErrors (flattened) ──────────────────────────────────────


def test_get_issues_dir_errors_raises_when_issues_dir_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ConfigError is raised when config has fieldkit_home but no github_repo."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fk\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_github_repo

    with pytest.raises(ConfigError, match=r"."):
        get_github_repo()


def test_get_issues_dir_errors_raises_when_config_file_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigError is raised when config.yaml does not exist."""
    missing = tmp_path / "nonexistent.yaml"
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", missing)

    from fieldkit.config import get_github_repo

    with pytest.raises(ConfigError, match=r"."):
        get_github_repo()


def test_get_issues_dir_errors_raises_when_github_repo_is_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ConfigError is raised when github_repo is present but blank/whitespace."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("fieldkit_home: /tmp/fk\ngithub_repo: '   '\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    from fieldkit.config import get_github_repo

    with pytest.raises(ConfigError, match=r"."):
        get_github_repo()


# ---------------------------------------------------------------------------
# historic regression: make install-skills workspace detection
# The Makefile must use get_fieldkit_home() (not .parent) to find the workspace
# containing .opencode/.claude.  Regression test: fieldkit_home itself, not its
# parent, is where tool-config dirs live.
# ---------------------------------------------------------------------------


def test_bug_1398_get_fieldkit_home_not_parent_has_tool_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_fieldkit_home() points at the dir with .opencode/.claude, not its parent.

    The Makefile install-skills target previously used get_fieldkit_home().parent,
    which never has .opencode — causing the sync to be silently skipped (historic regression).
    This test pins the invariant: the home dir itself must be the workspace root.
    """
    # Arrange: a fake fieldkit_home with an .opencode subdirectory
    fake_home = tmp_path / "fieldkit-home"
    fake_home.mkdir()
    (fake_home / ".opencode").mkdir()

    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"fieldkit_home: {fake_home}\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    workspace = get_fieldkit_home()

    # The workspace itself has .opencode — not workspace.parent
    assert (workspace / ".opencode").is_dir(), (
        "get_fieldkit_home() must point at the dir containing .opencode, "
        "not its parent (historic regression: install-skills workspace detection)"
    )
    assert not (workspace.parent / ".opencode").is_dir(), (
        "workspace.parent must NOT have .opencode — the Makefile must use "
        "get_fieldkit_home() directly, not get_fieldkit_home().parent"
    )


def test_bug_1398_dot_claude_also_accepted_as_tool_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace with only .claude/ (no .opencode/) is also valid for skill sync.

    The fixed install-skills target accepts either .opencode/ or .claude/ — both
    are valid tool-config directories that fieldkit skill install targets.
    """
    fake_home = tmp_path / "fieldkit-home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()  # .opencode absent; .claude present

    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"fieldkit_home: {fake_home}\n", encoding="utf-8")
    monkeypatch.setattr(fieldkit.config._loader, "CONFIG_PATH", config_file)

    workspace = get_fieldkit_home()

    assert (workspace / ".claude").is_dir(), "get_fieldkit_home() must point at the dir containing .claude"
    # Validate the guard logic: at least one of .opencode/.claude must exist
    has_tool_config = (workspace / ".opencode").is_dir() or (workspace / ".claude").is_dir()
    assert has_tool_config, "workspace must have .opencode or .claude for install-skills to proceed"
