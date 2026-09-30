"""Local meeting discovery does not require Google provider dependencies."""

import json
from pathlib import Path

import pytest

from fieldkit.__main__ import main
from fieldkit.commands.meeting.cli import _COMMANDS
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS
from fieldkit.errors import MissingOptionalDependencyError

pytestmark = pytest.mark.unit


def test_provider_profiles_use_canonical_google_roots() -> None:
    result = {name: (entry.profile, entry.import_roots) for name, entry in _COMMANDS.items()}

    assert result == {
        "list": (None, ()),
        "link": ("google", GOOGLE_IMPORT_ROOTS),
        "note": ("google", GOOGLE_IMPORT_ROOTS),
        "open": ("google", GOOGLE_IMPORT_ROOTS),
    }


def _missing_profile(command: str, profile: str, roots: tuple[str, ...]) -> None:
    raise MissingOptionalDependencyError(command, profile, roots)


@pytest.mark.parametrize("argv", [["meeting", "--help"], ["meeting", "list", "--json"]])
def test_local_meeting_commands_do_not_check_google_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr("fieldkit.__main__.require_optional_profile", _missing_profile)
    monkeypatch.setattr("fieldkit.commands._lazy.require_optional_profile", _missing_profile)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.commands.meeting.list_cmd.get_fieldkit_home", lambda: tmp_path)

    result = main(argv)

    assert result == 0
    output = capsys.readouterr()
    if "list" in argv:
        assert json.loads(output.out)["count"] == 0
    assert "requires the 'google'" not in output.err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["link", "note", "open"])
def test_provider_meeting_commands_keep_actionable_google_gate(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fieldkit.__main__.require_optional_profile", _missing_profile)
    monkeypatch.setattr("fieldkit.commands._lazy.require_optional_profile", _missing_profile)

    result = main(["meeting", command, "--help"])

    assert result == 3
    assert "requires the 'google' optional profile" in capsys.readouterr().err
