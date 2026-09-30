"""JSON usage errors are handled once at the public dispatcher boundary."""

import json
from pathlib import Path

import pytest

from fieldkit import __main__
from fieldkit.commands.watch import slack_threads

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("option", ["--limit", "--threshold-hours", "--limit-per-account"])
def test_malformed_slack_numeric_argument_has_sanitized_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, option: str
) -> None:
    monkeypatch.setattr(__main__, "load_dotenv_safe", lambda: None)
    monkeypatch.setenv("FIELDKIT_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("FIELDKIT_DATA", str(tmp_path / "runtime"))
    called = False

    def unexpected_run(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("usage error must not invoke the watcher")

    monkeypatch.setattr(slack_threads, "_run_slack_threads", unexpected_run)

    result = __main__.main(["watch", "run", "slack-threads", "--json", option, "private-invalid-token"])

    captured = capsys.readouterr()
    assert result == 3
    assert json.loads(captured.out) == {"outcome": "invalid", "error": "invalid_usage", "exit_code": 3}
    assert captured.err == ""
    assert "private-invalid-token" not in captured.out
    assert called is False
    assert list(tmp_path.rglob("*")) == []


def test_non_json_usage_error_preserves_helpful_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(__main__, "load_dotenv_safe", lambda: None)

    result = __main__.main(["watch", "run", "slack-threads", "--limit", "not-a-number"])

    captured = capsys.readouterr()
    assert result == 3
    assert captured.out == ""
    assert "Error:" in captured.err


def test_json_token_after_option_terminator_does_not_select_json_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(__main__, "load_dotenv_safe", lambda: None)

    result = __main__.main(["watch", "run", "slack-threads", "--limit", "not-a-number", "--", "--json"])

    captured = capsys.readouterr()
    assert result == 3
    assert captured.out == ""
    assert "Error:" in captured.err
