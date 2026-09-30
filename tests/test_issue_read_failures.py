"""A failed GitHub read is not an empty issue query."""

import subprocess
from unittest.mock import Mock

import pytest

from fieldkit.__main__ import main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("command", [["list"], ["show", "fieldkit-42"], ["board"]])
@pytest.mark.parametrize(
    "failure",
    ["provider", "auth", "timeout", "missing", "decode", "invalid-json", "empty-json", "object-json", "bad-item"],
)
def test_issue_reads_fail_closed(
    command: list[str], failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: False)
    monkeypatch.setattr("fieldkit.commands.issue.cli.get_github_repo", lambda: "example/fieldkit-cli")
    process = Mock(returncode=0, stdout="[]", stderr="")
    call = Mock(return_value=process)
    if failure in {"provider", "auth"}:
        process.returncode = 4 if failure == "auth" else 1
        process.stderr = "private-provider-diagnostic"
    elif failure == "timeout":
        call.side_effect = subprocess.TimeoutExpired(["gh"], 1, stderr="private-provider-diagnostic")
    elif failure == "missing":
        call.side_effect = FileNotFoundError("private-provider-diagnostic")
    elif failure == "decode":
        call.side_effect = UnicodeDecodeError(
            "utf-8",
            b"private-provider-diagnostic\xff",
            27,
            28,
            "invalid start byte",
        )
    elif failure == "invalid-json":
        process.stdout = "private-provider-diagnostic"
    elif failure == "object-json":
        process.stdout = "{}"
    elif failure == "bad-item":
        process.stdout = "[{}]"
    else:
        process.stdout = ""
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", call)
    result = main(["issue", *command, "--json"])
    expected = 2 if failure == "auth" else 1 if failure in {"provider", "timeout", "decode"} else 3
    assert result == expected
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-provider-diagnostic" not in output.err
    assert "Not found" not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "responses",
    [
        [Mock(returncode=0, stdout="{}", stderr="")],
        [
            Mock(returncode=0, stdout='[{"number": 7, "title": "M1"}]', stderr=""),
            Mock(returncode=0, stdout="[{}]", stderr=""),
        ],
    ],
)
def test_sync_milestone_rejects_malformed_reads_without_output(
    responses: list[Mock], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: False)
    monkeypatch.setattr("fieldkit.commands.issue.cli.get_github_repo", lambda: "example/fieldkit-cli")
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", Mock(side_effect=responses))

    result = main(["issue", "sync-milestone", "M1", "--state", "queued", "--dry-run", "--json"])

    output = capsys.readouterr()
    assert result == 3
    assert output.out == ""
    assert "Traceback" not in output.err


def test_board_later_read_failure_does_not_emit_partial_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: False)
    monkeypatch.setattr("fieldkit.commands.issue.cli.get_github_repo", lambda: "example/fieldkit-cli")
    calls = Mock(
        side_effect=[
            Mock(returncode=0, stdout="[]", stderr=""),
            Mock(returncode=1, stdout="", stderr="private-provider-diagnostic"),
        ]
    )
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", calls)

    result = main(["issue", "board", "--json"])

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "private-provider-diagnostic" not in output.err
