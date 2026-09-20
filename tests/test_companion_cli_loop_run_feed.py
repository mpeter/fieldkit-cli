"""Behaviour-pinning tests for fieldkit.commands.companion.cli: feed, run, loop.

Track B CRAP cleanup — these three Click commands had 0% CI coverage. Tests
patch at the source module the deferred in-function imports resolve against
(e.g. ``fieldkit.config.get_companion_tier``), never the call site, and use
``unittest.mock.patch`` context managers exclusively (monkeypatch increments
the governance-invariants advisory counter).
"""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.commands.companion.cli import cli
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.loop import LoopResult
from fieldkit.companion.runner import ActionResult

pytestmark = pytest.mark.unit

_HOME = Path("/fake/fieldkit-home")
_DATA = Path("/fake/fieldkit-data")


def _item(
    item_id: str = "i1",
    account: str | None = None,
    severity: str = "critical",
    summary: str = "something happened",
    evidence_path: str = "watchers/x.md",
    suggested_skill: str | None = None,
) -> AttentionItem:
    return AttentionItem(
        item_id=item_id,
        source="pursuit-stalls",
        account=account,
        severity=severity,
        summary=summary,
        evidence_path=evidence_path,
        suggested_skill=suggested_skill,
        observed_at="2026-08-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_feed_default_emits_json_lines_in_order() -> None:
    items = [_item("i1"), _item("i2")]
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()) as mock_retired,
        patch("fieldkit.companion.feed.get_feed", return_value=items) as mock_get_feed,
    ):
        result = CliRunner().invoke(cli, ["feed"], catch_exceptions=False)

    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert '"item_id": "i1"' in lines[0]
    assert '"item_id": "i2"' in lines[1]
    mock_retired.assert_called_once_with(_DATA)
    mock_get_feed.assert_called_once_with(
        _HOME,
        _DATA,
        since_cursor=True,
        account_slug=None,
        suppressed=set(),
    )


@pytest.mark.unit
def test_feed_no_items_json_mode_prints_nothing() -> None:
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()),
        patch("fieldkit.companion.feed.get_feed", return_value=[]),
    ):
        result = CliRunner().invoke(cli, ["feed"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output == ""


@pytest.mark.unit
def test_feed_all_flag_bypasses_cursor() -> None:
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()),
        patch("fieldkit.companion.feed.get_feed", return_value=[]) as mock_get_feed,
    ):
        CliRunner().invoke(cli, ["feed", "--all"], catch_exceptions=False)

    assert mock_get_feed.call_args.kwargs["since_cursor"] is False


@pytest.mark.unit
def test_feed_account_flag_filters() -> None:
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()),
        patch("fieldkit.companion.feed.get_feed", return_value=[]) as mock_get_feed,
    ):
        CliRunner().invoke(cli, ["feed", "--account", "acme"], catch_exceptions=False)

    assert mock_get_feed.call_args.kwargs["account_slug"] == "acme"


@pytest.mark.unit
def test_feed_markdown_with_items_formats_account_and_skill() -> None:
    items = [
        _item("i1", account="acme", severity="critical", summary="pursuit stalled", suggested_skill="pursuit-advance"),
        _item("i2", account=None, severity="info", summary="fyi note", suggested_skill=None),
    ]
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()),
        patch("fieldkit.companion.feed.get_feed", return_value=items),
    ):
        result = CliRunner().invoke(cli, ["feed", "--markdown"], catch_exceptions=False)

    assert result.exit_code == 0
    output = result.output
    assert "# Attention feed" in output
    assert "**CRITICAL** `acme` pursuit stalled → `pursuit-advance`" in output
    assert "evidence: watchers/x.md" in output
    assert "**INFO** fyi note" in output
    # item2 has no account and no skill: no backtick account marker, no arrow.
    assert "**INFO** `" not in output
    assert "fyi note →" not in output


@pytest.mark.unit
def test_feed_markdown_no_items_prints_message_and_skips_header() -> None:
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=_HOME),
        patch("fieldkit.config.get_fieldkit_data", return_value=_DATA),
        patch("fieldkit.companion.suppress.retired_item_ids", return_value=set()),
        patch("fieldkit.companion.feed.get_feed", return_value=[]),
    ):
        result = CliRunner().invoke(cli, ["feed", "--markdown"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output == "No new attention items.\n"
    assert "# Attention feed" not in result.output


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_no_command_exits_3_and_never_calls_run_action() -> None:
    with patch("fieldkit.companion.runner.run_action") as mock_run_action:
        result = CliRunner().invoke(cli, ["run"], catch_exceptions=False)

    assert result.exit_code == 3
    assert "no command given" in result.stderr
    assert mock_run_action.call_count == 0


@pytest.mark.unit
def test_run_denied_exits_3_and_suppresses_stdout() -> None:
    action_result = ActionResult(
        argv=["pursuit", "health"],
        exit_code=3,
        denied=True,
        stdout="should not appear",
        stderr="",
    )
    with patch("fieldkit.companion.runner.run_action", return_value=action_result):
        result = CliRunner().invoke(cli, ["run", "--item-id", "abc", "--", "pursuit", "health"], catch_exceptions=False)

    assert result.exit_code == 3
    assert "denied: pursuit health" in result.stderr
    assert "should not appear" not in result.output


@pytest.mark.unit
def test_run_success_prints_stdout_exits_zero_and_forwards_item_id() -> None:
    action_result = ActionResult(argv=["pursuit", "health"], exit_code=0, denied=False, stdout="hello out", stderr="")
    with patch("fieldkit.companion.runner.run_action", return_value=action_result) as mock_run_action:
        result = CliRunner().invoke(
            cli, ["run", "--item-id", "abc123", "--", "pursuit", "health"], catch_exceptions=False
        )

    assert result.exit_code == 0
    assert result.output == "hello out"
    assert mock_run_action.call_args.args[0] == ["pursuit", "health"]
    assert mock_run_action.call_args.kwargs["item_id"] == "abc123"


@pytest.mark.unit
def test_run_stderr_and_nonzero_exit_code_propagate() -> None:
    action_result = ActionResult(argv=["false"], exit_code=5, denied=False, stdout="", stderr="boom")
    with patch("fieldkit.companion.runner.run_action", return_value=action_result):
        result = CliRunner().invoke(cli, ["run", "--", "false"], catch_exceptions=False)

    assert result.exit_code == 5
    assert result.stdout == ""
    assert "boom" in result.stderr


@pytest.mark.unit
def test_run_zero_exit_code_does_not_raise() -> None:
    action_result = ActionResult(argv=["true"], exit_code=0, denied=False, stdout="", stderr="")
    with patch("fieldkit.companion.runner.run_action", return_value=action_result):
        result = CliRunner().invoke(cli, ["run", "--", "true"], catch_exceptions=False)

    assert result.exit_code == 0
    assert result.output == ""


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------


@contextmanager
def _patch_loop_env(*, configured_tier: str, loop_result: LoopResult):
    """Patch every companion-loop dependency and yield the run_once mock."""
    with ExitStack() as stack:
        stack.enter_context(patch("fieldkit.config.get_companion_tier", return_value=configured_tier))
        stack.enter_context(patch("fieldkit.config.get_companion_act_allowlist", return_value=[]))
        stack.enter_context(patch("fieldkit.config.get_fieldkit_home", return_value=_HOME))
        stack.enter_context(patch("fieldkit.config.get_fieldkit_data", return_value=_DATA))
        mock_run_once: MagicMock = stack.enter_context(
            patch("fieldkit.companion.loop.run_once", return_value=loop_result)
        )
        yield mock_run_once


@pytest.mark.unit
def test_loop_no_override_uses_configured_tier() -> None:
    loop_result = LoopResult(tier="propose")
    with _patch_loop_env(configured_tier="propose", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_run_once.call_args.kwargs["tier"] == "propose"
    assert mock_run_once.call_args.kwargs["dry_run"] is False
    assert mock_run_once.call_args.args == (_HOME, _DATA)
    assert "tier=propose" in result.output
    assert "[dry-run]" not in result.output


@pytest.mark.parametrize("override", ["propose", "read"])
@pytest.mark.unit
def test_loop_override_at_or_below_configured_is_accepted(override: str) -> None:
    loop_result = LoopResult(tier=override)  # type: ignore[arg-type]
    with _patch_loop_env(configured_tier="act", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop", "--tier", override], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_run_once.call_args.kwargs["tier"] == override


@pytest.mark.unit
def test_loop_override_escalation_is_rejected_and_never_runs() -> None:
    loop_result = LoopResult(tier="read")
    with _patch_loop_env(configured_tier="read", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop", "--tier", "act"], catch_exceptions=False)

    assert result.exit_code == EXIT_DATA
    assert "exceeds configured tier" in result.stderr
    assert mock_run_once.call_count == 0


@pytest.mark.unit
def test_loop_override_unknown_value_is_rejected_and_never_runs() -> None:
    loop_result = LoopResult(tier="read")
    with _patch_loop_env(configured_tier="act", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop", "--tier", "bogus"], catch_exceptions=False)

    assert result.exit_code == EXIT_DATA
    assert "unknown --tier" in result.stderr
    assert mock_run_once.call_count == 0


@pytest.mark.parametrize("override", ["ACT", " act ", "Act"])
@pytest.mark.unit
def test_loop_override_is_case_and_whitespace_normalized(override: str) -> None:
    loop_result = LoopResult(tier="act")
    with _patch_loop_env(configured_tier="act", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop", "--tier", override], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_run_once.call_args.kwargs["tier"] == "act"


@pytest.mark.unit
def test_loop_invalid_configured_tier_fails_closed_to_read() -> None:
    """Defensive branch: an invalid configured tier (no --tier given) resets to read."""
    loop_result = LoopResult(tier="read")
    with _patch_loop_env(configured_tier="not-a-real-tier", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_run_once.call_args.kwargs["tier"] == "read"


@pytest.mark.unit
def test_loop_json_flag_emits_result_to_dict() -> None:
    import json

    loop_result = LoopResult(tier="act", triaged=1, proposed=2, enriched=3, acted=1)
    with _patch_loop_env(configured_tier="act", loop_result=loop_result):
        result = CliRunner().invoke(cli, ["loop", "--json"], catch_exceptions=False)

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == loop_result.to_dict()


@pytest.mark.unit
def test_loop_dry_run_flag_forwarded_and_shown_as_prefix() -> None:
    loop_result = LoopResult(tier="propose")
    with _patch_loop_env(configured_tier="propose", loop_result=loop_result) as mock_run_once:
        result = CliRunner().invoke(cli, ["loop", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_run_once.call_args.kwargs["dry_run"] is True
    assert result.output.startswith("[dry-run] ")


@pytest.mark.unit
def test_loop_once_flag_accepted_without_changing_output() -> None:
    loop_result = LoopResult(tier="read")
    with _patch_loop_env(configured_tier="read", loop_result=loop_result):
        without_once = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)
    with _patch_loop_env(configured_tier="read", loop_result=loop_result):
        with_once = CliRunner().invoke(cli, ["loop", "--once"], catch_exceptions=False)

    assert with_once.exit_code == without_once.exit_code == 0
    assert with_once.output == without_once.output


@pytest.mark.unit
def test_loop_auth_failures_exit_code_2() -> None:
    loop_result = LoopResult(tier="act", auth_failures=1)
    with _patch_loop_env(configured_tier="act", loop_result=loop_result):
        result = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)

    assert result.exit_code == EXIT_AUTH


@pytest.mark.unit
def test_loop_partial_without_auth_failures_exit_code_1() -> None:
    loop_result = LoopResult(tier="act", enrich_failures=1, auth_failures=0)
    with _patch_loop_env(configured_tier="act", loop_result=loop_result):
        result = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)

    assert result.exit_code == EXIT_PARTIAL


@pytest.mark.unit
def test_loop_auth_failures_take_priority_over_partial() -> None:
    loop_result = LoopResult(tier="act", enrich_failures=1, auth_failures=1)
    with _patch_loop_env(configured_tier="act", loop_result=loop_result):
        result = CliRunner().invoke(cli, ["loop"], catch_exceptions=False)

    assert result.exit_code == EXIT_AUTH
