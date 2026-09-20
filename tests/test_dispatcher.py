"""Smoke tests for the _COMMANDS dispatcher in fieldkit.__main__.

historic regression: Ensures every entry in _COMMANDS maps to an importable module
with a `cli` attribute. Catches stale or wrong module paths at CI time
rather than at runtime when the user runs a command.
"""

import importlib
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import _COMMANDS, _LazyGroup, cli
from fieldkit.config.optional_dependencies import require_optional_profile
from fieldkit.errors import MissingOptionalDependencyError

pytestmark = pytest.mark.unit


# ── TestDispatcher (flattened) ──────────────────────────────────────────────


@pytest.mark.parametrize("cmd_name,entry", list(_COMMANDS.items()))
def test_dispatcher_command_module_importable(cmd_name: str, entry: tuple[str, str]) -> None:
    """Every _COMMANDS entry must import cleanly and expose a `cli` attribute."""
    _description, module_path = entry
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        pytest.fail(f"_COMMANDS[{cmd_name!r}] module {module_path!r} failed to import: {exc}")

    assert hasattr(module, "cli"), (
        f"_COMMANDS[{cmd_name!r}] module {module_path!r} has no `cli` attribute. "
        "Check the module path in fieldkit.__main__._COMMANDS."
    )


def test_top_level_help_does_not_probe_or_import_optional_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    find_spec = Mock(side_effect=AssertionError("top-level help probed an optional SDK"))
    import_module = Mock(side_effect=AssertionError("top-level help imported a command module"))
    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", find_spec)
    monkeypatch.setattr("fieldkit.__main__.importlib.import_module", import_module)

    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "meeting" in result.output
    find_spec.assert_not_called()
    import_module.assert_not_called()


def test_optional_command_checks_declared_roots_before_import(monkeypatch: pytest.MonkeyPatch) -> None:
    imported = Mock(side_effect=AssertionError("missing profile imported its command module"))
    monkeypatch.setattr(
        "fieldkit.config.optional_dependencies.importlib.util.find_spec",
        lambda root: None if root == "google.auth" else Mock(),
    )
    monkeypatch.setattr("fieldkit.__main__.importlib.import_module", imported)

    with pytest.raises(MissingOptionalDependencyError) as exc_info:
        _LazyGroup().get_command(Mock(), "meeting")

    assert exc_info.value.command == "meeting"
    assert exc_info.value.profile == "google"
    assert exc_info.value.missing_import_roots == ("google.auth",)
    imported.assert_not_called()


def test_unrelated_import_error_is_not_reclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", lambda _root: Mock())
    monkeypatch.setattr(
        "fieldkit.__main__.importlib.import_module",
        Mock(side_effect=ImportError("broken internal import")),
    )

    with pytest.raises(ImportError, match="broken internal import"):
        _LazyGroup().get_command(Mock(), "meeting")


def test_unrelated_probe_import_error_is_not_reclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_probe(_root: str) -> None:
        raise ModuleNotFoundError("broken parent import", name="broken_parent_helper")

    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", broken_probe)

    with pytest.raises(ModuleNotFoundError, match="broken parent import"):
        require_optional_profile("meeting", "google", ("google.auth",))


def test_loaded_module_without_spec_satisfies_optional_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("example_optional_dependency")
    probe = Mock(side_effect=ValueError(f"{module.__name__}.__spec__ is None"))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", probe)

    result = require_optional_profile("example", "example", (module.__name__,))

    assert result is None
    probe.assert_called_once_with(module.__name__)


def test_auth_help_does_not_import_google_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.auth.cli import cli as auth_cli

    imported = Mock(side_effect=AssertionError("auth help imported a leaf command"))
    monkeypatch.setattr("fieldkit.commands._lazy.importlib.import_module", imported)

    result = CliRunner().invoke(auth_cli, ["--help"])

    assert result.exit_code == 0
    assert "google" in result.output
    imported.assert_not_called()


def test_auth_google_leaf_declares_google_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.auth.cli import cli as auth_cli

    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", lambda _root: None)

    result = CliRunner().invoke(auth_cli, ["google", "--help"])

    assert isinstance(result.exception, MissingOptionalDependencyError)
    assert result.exception.command == "auth google"
    assert result.exception.profile == "google"
