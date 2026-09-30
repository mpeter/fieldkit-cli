"""Read-tier feed inspection must not advance the persistent cursor."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fieldkit.commands.companion.cli import cli
from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tier", ["read", "propose", "act"])
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["companion", "feed"], False),
        (["companion", "feed", "--json"], False),
        (["companion", "feed", "--all"], True),
        (["companion", "feed", "--all", "--json"], True),
        (["companion", "feed", "--", "--all"], False),
        (["companion", "feed", "--account", "--all"], False),
        (["companion", "feed", "--account=--all"], False),
        (["companion", "feed", "--account", "acme", "--all"], True),
        (["companion", "feed", "--all", "--account", "acme"], True),
        (["companion", "feed", "--all=true"], False),
    ],
)
def test_read_table_requires_cursor_free_feed(argv: list[str], expected: bool, tier: str) -> None:
    result = is_allowed(argv, tier, NO_ACT_POLICY)

    assert result is expected


def test_cursor_free_feed_reads_attention_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "workspace"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: home)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: data)

    result = CliRunner().invoke(cli, ["feed", "--all", "--json"])

    assert result.exit_code == 0
    assert result.output.strip()
    assert list(home.rglob("*")) == []
    assert list(data.rglob("*")) == []


@pytest.mark.parametrize(
    "tokens",
    [
        ["--all"],
        ["--account", "acme", "--all"],
        ["--all", "--account=acme"],
        ["--json", "--all", "--markdown"],
    ],
)
def test_authorized_feed_invocation_really_disables_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokens: list[str]
) -> None:
    result = is_allowed(["companion", "feed", *tokens], "read", NO_ACT_POLICY)
    assert result is True
    read = MagicMock(return_value=[])
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.companion.feed.get_feed", read)

    invocation = CliRunner().invoke(cli, ["feed", *tokens])

    assert invocation.exit_code == 0
    assert read.call_args.kwargs["since_cursor"] is False
