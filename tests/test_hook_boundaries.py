"""Hook policy survives the removal of checkout-wide Tach roots."""

from pathlib import Path

import pytest

from scripts.check_hook_boundaries import check_boundaries

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("source", "allowed"),
    [
        ("import fieldkit.config", True),
        ("from fieldkit.enrich.constants import GARBAGE_NAMES", True),
        ("from fieldkit.errors import FieldkitError", True),
        ("from fieldkit.publication_policy import main", True),
        ("from hooks._common import block", True),
        ("from ._common import block", True),
        ("import fieldkit.commands.sf", False),
        ("from fieldkit.commands import sf", False),
        ("from fieldkit import commands", False),
        ("from fieldkit.pursuit.io import load", False),
        ("import fieldkit.llm as llm", False),
    ],
)
def test_hook_dependency_policy(tmp_path: Path, source: str, allowed: bool) -> None:
    (tmp_path / "tach.toml").write_text(
        "\n".join(
            f'[[modules]]\npath = "{module}"'
            for module in (
                "fieldkit",
                "fieldkit.config",
                "fieldkit.enrich",
                "fieldkit.errors",
                "fieldkit.commands",
                "fieldkit.pursuit",
                "fieldkit.llm",
            )
        ),
        encoding="utf-8",
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "new_hook.py").write_text(source, encoding="utf-8")

    result = check_boundaries(tmp_path)

    assert (result == []) is allowed
    if not allowed:
        assert all("hooks/new_hook.py:1: forbidden import fieldkit" in error for error in result)


def test_domain_cannot_import_hooks(tmp_path: Path) -> None:
    (tmp_path / "tach.toml").write_text('[[modules]]\npath = "fieldkit"', encoding="utf-8")
    src = tmp_path / "src/fieldkit"
    src.mkdir(parents=True)
    (src / "domain.py").write_text("from hooks import _common", encoding="utf-8")

    result = check_boundaries(tmp_path)

    assert result
    assert all("src/fieldkit/domain.py:1: forbidden import hooks" in error for error in result)


def test_current_repository_satisfies_hook_policy() -> None:
    result = check_boundaries(Path(__file__).resolve().parents[1])

    assert result == []
