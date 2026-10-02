"""Tests for scripts/check_pr_title.py.

``scripts/`` is added to sys.path by conftest.py, so the module can be
imported directly as ``check_pr_title``.
"""

from pathlib import Path

import check_pr_title
import pytest

pytestmark = pytest.mark.unit

_TYPES = ("feat", "fix", "docs", "chore")


def _config(tmp_path: Path, contents: str) -> Path:
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(contents, encoding="utf-8")
    return config


def test_allowed_types_match_the_repository_commit_msg_hook() -> None:
    types = check_pr_title.allowed_types()

    assert "feat" in types
    assert "fix" in types
    assert all(item and item == item.strip() for item in types)


def test_allowed_types_stop_at_the_next_hook(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "repos:\n"
        "  - repo: https://example.com/hooks\n"
        "    hooks:\n"
        "      - id: conventional-pre-commit\n"
        "        stages: [commit-msg]\n"
        "      - id: other-hook\n"
        "        args: [feat, fix]\n",
    )

    with pytest.raises(ValueError, match="no args list"):
        check_pr_title.allowed_types(config)


@pytest.mark.parametrize(
    "title",
    [
        "fix: drop the stale stage",
        "feat(web): add a tab",
        "feat!: remove developer automation from the public CLI",
        "chore(deps)!: raise the Python floor",
    ],
)
def test_title_error_accepts_conventional_subjects(title: str) -> None:
    assert check_pr_title.title_error(title, _TYPES) is None


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Remove developer automation", "not a Conventional Commits subject"),
        ("feature: add a tab", "not a Conventional Commits subject"),
        ("fix:missing space", "not a Conventional Commits subject"),
        ("fix(): empty scope", "not a Conventional Commits subject"),
        ("fix: ", "not a Conventional Commits subject"),
    ],
)
def test_title_error_rejects_malformed_subjects(title: str, reason: str) -> None:
    error = check_pr_title.title_error(title, _TYPES)

    assert error is not None
    assert reason in error


@pytest.mark.parametrize(("title", "expected"), [("docs: explain the merge policy", 0), ("Merge policy", 1)])
def test_main_exit_status_follows_the_title(title: str, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_TITLE", title)

    assert check_pr_title.main() == expected


def test_main_reports_an_unreadable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_pr_title, "PRE_COMMIT_CONFIG", tmp_path / "missing.yaml")
    monkeypatch.setenv("PR_TITLE", "fix: anything")

    assert check_pr_title.main() == 3
