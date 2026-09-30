"""Tests for fieldkit watch run --all and cron install logic."""

# Import the watch cli MODULE (not the Click group) to access _WATCHER_ORDER.
import importlib
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from inspect import signature
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.watch.cli import cli
from fieldkit.commands.watch.cli import cli as _cli_group  # noqa: F401
from fieldkit.errors import AuthError, LLMError, LLMErrorCategory
from fieldkit.watch import close_date_countdown as countdown_domain
from fieldkit.watch.integration_plan import IntegrationPlan
from fieldkit.watch.slack_threads import SlackRunOutcome
from fieldkit.watch.status import WatcherDailySnapshot, WatcherOutcome, WatcherRunResult

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["critical", "warning", "notice"])
def test_run_all_contract_expiry_defaults_match_leaf(name: str) -> None:
    from fieldkit.commands.watch.contract_expiry import cli as contract_expiry_cli
    from fieldkit.watch.contract_expiry import _run_contract_expiry

    domain_signature = signature(_run_contract_expiry)
    defaults = {parameter.name: parameter.default for parameter in contract_expiry_cli.params}
    assert domain_signature.parameters[name].default == defaults[f"threshold_{name}"]


_watch_cli_mod = importlib.import_module("fieldkit.commands.watch.cli")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_zero() -> dict[str, int]:
    return {
        "waiting-on-tracker": 0,
        "pursuit-stalls": 0,
        "close-date-countdown": 0,
        "contract-expiry": 0,
        "backstory-health": 0,
        "slack-threads": 0,
        "draft-queue": 0,
    }


def _slack_outcome(exit_code: int) -> SlackRunOutcome:
    return SlackRunOutcome(
        run=_typed_result(exit_code),
        records_checked=0,
        alerts_generated=0,
        failures=int(exit_code != 0),
        auth_error=exit_code == 2,
        provider_error=False,
        elapsed_seconds=0.0,
        dry_run=False,
    )


def _typed_result(code: int) -> WatcherRunResult:
    if code == 0:
        return WatcherRunResult("ok", True, "written")
    if code == 1:
        return WatcherRunResult("partial", True, "written")
    if code == 2:
        return WatcherRunResult("fatal", False, None, 2)
    return WatcherRunResult("fatal", False, None, 3)


# ---------------------------------------------------------------------------
# run --all sequencing and exit code aggregation
# ---------------------------------------------------------------------------


@contextmanager
def _patch_watcher_runs(
    outcomes: Mapping[str, object], calls: list[tuple[str, dict[str, object]]]
) -> Generator[None, None, None]:
    """Patch direct watcher-domain calls and record their kwargs."""
    targets = {
        "waiting-on-tracker": "fieldkit.watch.waiting_on_tracker._run",
        "pursuit-stalls": "fieldkit.watch.pursuit_stalls._run_pursuit_stalls",
        "close-date-countdown": "fieldkit.watch.close_date_countdown._run_countdown",
        "contract-expiry": "fieldkit.watch.contract_expiry._run_contract_expiry",
        "backstory-health": "fieldkit.watch.backstory_health._run_backstory_health",
        "slack-threads": "fieldkit.watch.slack_threads._run_slack_threads",
        "draft-queue": "fieldkit.watch.draft_queue._run_draft_queue",
    }

    with ExitStack() as stack:
        for name, target in targets.items():

            def _run(*, _name: str = name, **kwargs: object) -> object:
                calls.append((_name, kwargs))
                outcome = outcomes.get(_name, 0)
                if isinstance(outcome, Exception):
                    raise outcome
                if callable(outcome):
                    callback: Callable[[], object] = outcome
                    outcome = callback()
                if _name == "slack-threads" and type(outcome) is int:
                    return _slack_outcome(outcome)
                return _typed_result(outcome) if type(outcome) is int else outcome

            stack.enter_context(patch(target, side_effect=_run))
        yield


def _run_all_run_run_all(
    outcomes: Mapping[str, object],
    calls: list[tuple[str, dict[str, object]]],
    args: list[str] | None = None,
    brief_rc: int = 0,
) -> Result:
    outer_runner = CliRunner()
    plan = IntegrationPlan(
        preflight_services=(),
        optional_watchers=("backstory-health", "draft-queue", "slack-threads"),
        calendar=False,
        llm=False,
        skipped=(),
    )
    with (
        _patch_watcher_runs(outcomes, calls),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(brief_rc)),
    ):
        return outer_runner.invoke(cli, ["run", "--all"] + (args or []), catch_exceptions=False)


def test_run_all_calls_all_watchers_in_correct_order() -> None:
    """run --all invokes every domain function in the production order."""
    calls: list[tuple[str, dict[str, object]]] = []
    result = _run_all_run_run_all(_all_zero(), calls)
    assert [name for name, _kwargs in calls] == _watch_cli_mod._WATCHER_ORDER
    assert result.exit_code == 0


def test_run_all_local_plan_skips_optional_watchers_explicitly() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    plan = IntegrationPlan(
        (),
        (),
        False,
        False,
        (
            "backstory-health: not configured",
            "draft-queue: not configured",
            "slack-threads: not selected",
        ),
    )
    with (
        _patch_watcher_runs(_all_zero(), calls),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(0)),
    ):
        result = CliRunner().invoke(cli, ["run", "--all"])

    assert result.exit_code == 0
    assert [name for name, _kwargs in calls] == [
        "waiting-on-tracker",
        "pursuit-stalls",
        "close-date-countdown",
        "contract-expiry",
    ]
    assert "not run: backstory-health: not configured" in result.output


def test_run_all_slack_flag_is_part_of_canonical_selection() -> None:
    plan = IntegrationPlan((), (), False, False, ())
    with patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan) as build:
        result = CliRunner().invoke(cli, ["run", "--all", "--slack", "--dry-run"])

    assert result.exit_code in {0, 1}
    assert build.call_args.kwargs["slack_requested"] is True


def test_run_all_uses_effective_watcher_defaults() -> None:
    """Direct dispatch preserves each leaf command's effective default arguments."""
    calls: list[tuple[str, dict[str, object]]] = []

    result = _run_all_run_run_all(_all_zero(), calls)

    assert result.exit_code == 0
    assert calls == [
        ("waiting-on-tracker", {"threshold": 7, "dry_run": False, "as_json": False}),
        ("pursuit-stalls", {"threshold": 14, "account": None, "dry_run": False, "force": False}),
        (
            "close-date-countdown",
            {
                "threshold_red": countdown_domain._DEFAULT_RED_DAYS,
                "threshold_yellow": countdown_domain._DEFAULT_YELLOW_DAYS,
                "threshold_green": countdown_domain._DEFAULT_GREEN_DAYS,
                "account_filter": None,
                "dry_run": False,
                "as_json": False,
            },
        ),
        (
            "contract-expiry",
            {"account_filter": None, "dry_run": False, "as_json": False},
        ),
        ("backstory-health", {"threshold": 60, "account": None, "dry_run": False, "as_json": False}),
        (
            "slack-threads",
            {
                "threshold_hours": 48,
                "account": None,
                "limit": 50,
                "limit_per_account": None,
                "dry_run": False,
            },
        ),
        ("draft-queue", {"dry_run": False, "account": None, "as_json": False}),
    ]


def test_run_all_leaf_countdown_defaults_match_domain_constants() -> None:
    """The leaf Click defaults use the countdown domain's canonical values."""
    from fieldkit.commands.watch.close_date_countdown import cli as countdown_cli

    defaults = {parameter.name: parameter.default for parameter in countdown_cli.params}

    assert defaults["threshold_red"] == countdown_domain._DEFAULT_RED_DAYS
    assert defaults["threshold_yellow"] == countdown_domain._DEFAULT_YELLOW_DAYS
    assert defaults["threshold_green"] == countdown_domain._DEFAULT_GREEN_DAYS


def test_run_all_discards_domain_streams_and_restores_aggregate_output() -> None:
    """Domain stdout/stderr stay hidden while the aggregate summary remains visible."""

    def noisy_domain() -> WatcherRunResult:
        print("domain stdout marker")
        print("domain stderr marker", file=sys.stderr)
        return _typed_result(0)

    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": noisy_domain}

    result = _run_all_run_run_all(outcomes, [])

    assert result.exit_code == 0
    assert "domain stdout marker" not in result.output
    assert "domain stderr marker" not in result.output
    assert "Watcher Summary:" in result.output


def test_invoke_watcher_returns_typed_result_and_discards_domain_streams(capsys: pytest.CaptureFixture[str]) -> None:
    """The direct-call boundary preserves an exact domain exit code without leaking output."""

    def noisy_domain() -> WatcherRunResult:
        print("domain stdout marker")
        print("domain stderr marker", file=sys.stderr)
        return _typed_result(3)

    rc = _watch_cli_mod._invoke_watcher("waiting-on-tracker", noisy_domain)

    assert rc == _typed_result(3)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("invalid_result", [False, None, "1"])
def test_invoke_watcher_treats_non_exact_integer_result_as_failure(invalid_result: object) -> None:
    rc = _watch_cli_mod._invoke_watcher("waiting-on-tracker", lambda: invalid_result)

    assert rc == WatcherRunResult("fatal", False, None)


@pytest.mark.parametrize("adapter", ["watcher", "brief"])
@pytest.mark.parametrize(
    "returned,expected",
    [
        (0, WatcherRunResult("fatal", False, None)),
        ("private-diagnostic-sentinel", WatcherRunResult("fatal", False, None)),
        (WatcherRunResult("ok", True, None), WatcherRunResult("fatal", True, None)),
        (WatcherRunResult("partial", True, "skipped"), WatcherRunResult("fatal", True, "skipped")),
    ],
)
def test_invalid_execution_evidence_has_a_fixed_adapter_diagnostic(
    adapter: str,
    returned: object,
    expected: WatcherRunResult,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if adapter == "watcher":
        result = _watch_cli_mod._invoke_watcher("waiting-on-tracker", lambda: returned)
        name = "waiting-on-tracker"
    else:
        with patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=returned):
            result, _elapsed = _watch_cli_mod._run_brief_step(dry_run=False, no_llm=True)
        name = "morning-brief"

    assert result == expected
    assert f"watcher={name} returned invalid execution evidence" in caplog.text
    assert "private-diagnostic-sentinel" not in caplog.text


def test_invoke_watcher_propagates_auth_error() -> None:
    def auth_failure() -> int:
        raise AuthError("refresh credentials")

    with pytest.raises(AuthError, match="refresh credentials"):
        _watch_cli_mod._invoke_watcher("waiting-on-tracker", auth_failure)


def test_invoke_watcher_maps_ordinary_exception_to_failure(caplog: pytest.LogCaptureFixture) -> None:
    def ordinary_failure() -> int:
        raise ValueError("watcher failure")

    rc = _watch_cli_mod._invoke_watcher("waiting-on-tracker", ordinary_failure)

    assert rc == WatcherRunResult("fatal", False, None)
    assert "watcher=waiting-on-tracker raised an unexpected exception" in caplog.text
    assert "watcher failure" not in caplog.text


def test_run_all_restores_streams_before_logging_ordinary_exception() -> None:
    """Ordinary errors are reported after hidden domain output and do not stop later domains."""

    def noisy_failure() -> int:
        print("domain stdout marker")
        print("domain stderr marker", file=sys.stderr)
        raise ValueError("watcher failure")

    calls: list[tuple[str, dict[str, object]]] = []
    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": noisy_failure}

    with patch(
        "fieldkit.commands.watch.cli.logging.error",
        side_effect=lambda message, *args: print(message % args, file=sys.stderr),
    ):
        result = _run_all_run_run_all(outcomes, calls)

    assert result.exit_code == 1
    assert "domain stdout marker" not in result.output
    assert "domain stderr marker" not in result.output
    assert "watcher=waiting-on-tracker raised an unexpected exception" in result.output
    assert "watcher failure" not in result.output
    assert [name for name, _kwargs in calls] == _watch_cli_mod._WATCHER_ORDER


def test_run_all_restores_streams_before_auth_error_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    """Auth diagnostics remain visible after domain stdout and stderr are discarded."""
    from fieldkit.__main__ import main

    def noisy_auth_failure() -> int:
        print("domain stdout marker")
        print("domain stderr marker", file=sys.stderr)
        raise AuthError("refresh credentials")

    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": noisy_auth_failure}
    with (
        _patch_watcher_runs(outcomes, []),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = main(["watch", "run", "--all"])

    captured = capsys.readouterr()
    assert result == 2
    assert "domain stdout marker" not in captured.out
    assert "domain stderr marker" not in captured.err
    assert "Auth error" in captured.err


def test_run_all_exit_code_is_max_of_watcher_codes() -> None:
    """Exit code must be max of all individual watcher exit codes."""
    name_to_rc = {
        "waiting-on-tracker": 0,
        "pursuit-stalls": 0,
        "close-date-countdown": 0,
        "contract-expiry": 0,
        "backstory-health": 3,
        "slack-threads": 1,
        "draft-queue": 0,
    }
    result = _run_all_run_run_all(name_to_rc, [], brief_rc=2)
    assert result.exit_code == 3


def test_run_all_exit_code_zero_when_all_succeed() -> None:
    """Exit code is 0 when all watchers return 0."""
    result = _run_all_run_run_all(_all_zero(), [])
    assert result.exit_code == 0


@pytest.mark.parametrize("watcher_code", [0, 1, 2, 3])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_run_all_status_write_failure_remains_nonpassing(watcher_code: int, allow_partial: bool) -> None:
    outcomes = _all_zero()
    outcomes["close-date-countdown"] = watcher_code
    plan = IntegrationPlan((), ("backstory-health", "draft-queue", "slack-threads"), False, False, ())
    args = ["run", "--all"]
    if allow_partial:
        args.append("--allow-partial")
    with (
        _patch_watcher_runs(outcomes, []),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="failed") as write_status,
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(0)),
    ):
        result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == max(1, watcher_code)
    assert "run status was not persisted" in result.output
    write_status.assert_called_once()


def test_run_all_maps_typed_slack_auth_outcome_to_exit_two() -> None:
    outcome = _slack_outcome(2)
    results: Mapping[str, object] = {**_all_zero(), "slack-threads": outcome}

    result = _run_all_run_run_all(results, [])

    assert outcome.run.failure_code == 2
    assert result.exit_code == 2


def test_run_all_propagates_partial_countdown_to_exit_and_persisted_outcome() -> None:
    outcomes = _all_zero()
    outcomes["close-date-countdown"] = 1
    calls: list[tuple[str, dict[str, object]]] = []
    plan = IntegrationPlan((), ("backstory-health", "draft-queue", "slack-threads"), False, False, ())

    with (
        _patch_watcher_runs(outcomes, calls),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written") as write_status,
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(0)),
    ):
        result = CliRunner().invoke(cli, ["run", "--all"], catch_exceptions=False)

    assert result.exit_code == 1
    assert [name for name, _kwargs in calls] == _watch_cli_mod._WATCHER_ORDER
    write_status.assert_called_once()
    assert write_status.call_args.kwargs["watcher"] == "run-all"
    assert write_status.call_args.kwargs["outcome"] == "partial"
    assert write_status.call_args.kwargs["failures"] == 1


@pytest.mark.parametrize("prior_outcome", ["ok", "partial", "fatal"])
def test_run_all_dry_run_ignores_prior_daily_outcome_and_executes_preview(prior_outcome: WatcherOutcome) -> None:
    from fieldkit.__main__ import main

    calls: list[tuple[str, dict[str, object]]] = []
    plan = IntegrationPlan((), ("backstory-health", "draft-queue", "slack-threads"), False, False, ())
    with (
        _patch_watcher_runs(_all_zero(), calls),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, prior_outcome)),
        patch("fieldkit.commands.watch.cli._run_preflight_guard") as preflight,
        patch("fieldkit.watch.status.write_run_status", return_value="written") as write_status,
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(0)),
    ):
        exit_code = main(["watch", "run", "--all", "--dry-run"])

    assert exit_code == 0
    assert [name for name, _kwargs in calls] == [
        name for name in _watch_cli_mod._WATCHER_ORDER if name != "slack-threads"
    ]
    assert all(kwargs["dry_run"] is True for _name, kwargs in calls)
    preflight.assert_not_called()
    write_status.assert_not_called()


@pytest.mark.parametrize("invalid_result", [False, None, "1"])
def test_run_all_treats_non_exact_integer_watcher_result_as_failure(invalid_result: object) -> None:
    """Bool, None, and string watcher results must fail closed."""
    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": invalid_result}

    result = _run_all_run_run_all(outcomes, [])

    assert result.exit_code == 1


def test_run_all_allow_partial_keeps_the_chain_running_after_completed_partial_pass() -> None:
    """A scheduled mother-hen pass may continue after a recorded partial watcher result."""
    name_to_rc = _all_zero()
    name_to_rc["pursuit-stalls"] = 1
    result = _run_all_run_run_all(name_to_rc, [], args=["--allow-partial"])

    assert result.exit_code == 0


def test_run_all_propagates_auth_errors() -> None:
    """An expired credential must reach cli_main() for exit-2 handling."""
    outcomes: Mapping[str, int | Exception] = {**_all_zero(), "waiting-on-tracker": AuthError("refresh credentials")}

    with pytest.raises(AuthError, match="refresh credentials"):
        _run_all_run_run_all(outcomes, [])


def test_run_all_auth_error_maps_to_exit_two_at_real_cli_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    """The top-level dispatcher maps an escaping watcher AuthError to exit 2."""
    from fieldkit.__main__ import main

    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": AuthError("refresh credentials")}
    with (
        _patch_watcher_runs(outcomes, []),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = main(["watch", "run", "--all"])

    assert result == 2
    assert "Auth error" in capsys.readouterr().err


@pytest.mark.parametrize("watcher", ["backstory-health", "slack-threads"])
@pytest.mark.parametrize("content", [b"accounts: [\n", b"accounts: {}\naccounts: {}\n", b"\xff"])
def test_run_all_invalid_account_configuration_maps_to_exit_three(
    tmp_path: Path, watcher: str, content: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid account configuration is not a retryable aggregate failure."""
    from fieldkit.__main__ import main

    config = tmp_path / "accounts.yaml"
    config.write_bytes(content)
    domain = importlib.import_module(f"fieldkit.watch.{watcher.replace('-', '_')}")
    run = domain._run_backstory_health if watcher == "backstory-health" else domain._run_slack_threads

    def read_invalid_config() -> object:
        if watcher == "backstory-health":
            return run(threshold=60, account=None, dry_run=False)
        return run(threshold_hours=48, account=None, limit=50, dry_run=False)

    outcomes: Mapping[str, object] = {**_all_zero(), watcher: read_invalid_config}
    plan = IntegrationPlan(preflight_services=(), optional_watchers=(watcher,), calendar=False, llm=False, skipped=())
    with (
        _patch_watcher_runs(outcomes, []),
        patch("fieldkit.config._accounts.get_config_path", return_value=config),
        patch.object(domain, "watcher_logging", return_value=nullcontext()),
        patch.object(domain, "write_run_status") as domain_status,
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=_typed_result(0)),
    ):
        result = main(["watch", "run", "--all"])

    assert result == 3
    assert "Config error" in capsys.readouterr().err
    domain_status.assert_not_called()
    assert config.read_bytes() == content


def test_run_all_dry_run_passes_flag_to_each_watcher() -> None:
    """--dry-run propagates to every watcher domain call."""
    calls: list[tuple[str, dict[str, object]]] = []
    result = _run_all_run_run_all(_all_zero(), calls, args=["--dry-run"])
    assert result.exit_code == 0
    assert all(kwargs["dry_run"] is True for _name, kwargs in calls)


def test_run_all_brief_configuration_error_maps_to_exit_three(capsys: pytest.CaptureFixture[str]) -> None:
    """The aggregate brief step preserves the same invalid-data category."""
    from fieldkit.__main__ import main
    from fieldkit.config import ConfigError

    plan = IntegrationPlan(preflight_services=(), optional_watchers=(), calendar=False, llm=False, skipped=())
    with (
        _patch_watcher_runs(_all_zero(), []),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.commands.brief.generate._run_generate_inner",
            side_effect=ConfigError("Invalid account configuration"),
        ),
    ):
        result = main(["watch", "run", "--all"])

    assert result == 3
    assert "Config error" in capsys.readouterr().err


@pytest.mark.parametrize(("category", "status"), [("auth", 2), ("rate-limit", 1), ("general", 3)])
def test_run_all_brief_llm_error_reaches_cli_category_mapping(
    capsys: pytest.CaptureFixture[str],
    category: LLMErrorCategory,
    status: int,
) -> None:
    from fieldkit.__main__ import main

    failure = LLMError("private-provider-payload-sentinel", category)
    plan = IntegrationPlan(preflight_services=(), optional_watchers=(), calendar=False, llm=False, skipped=())
    with (
        _patch_watcher_runs(_all_zero(), []),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written") as aggregate_status,
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.generate._run_generate_inner", side_effect=failure),
    ):
        result = main(["watch", "run", "--all"])

    assert result == status
    assert aggregate_status.call_args.kwargs["watcher"] == "run-all"
    assert aggregate_status.call_args.kwargs["outcome"] == "fatal"
    assert aggregate_status.call_args.kwargs["failures"] >= 1
    assert "private-provider-payload-sentinel" not in capsys.readouterr().err


def test_run_all_selected_llm_preflight_failure_stops_before_dispatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A selected LLM fails before any watcher or brief provider work begins."""
    calls: list[tuple[str, dict[str, object]]] = []
    plan = IntegrationPlan(("llm",), (), False, True, ())
    from fieldkit.__main__ import main

    with (
        _patch_watcher_runs(_all_zero(), calls),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=["llm credentials missing"]),
        patch("fieldkit.commands.brief.generate._run_generate_inner") as mock_generate,
    ):
        result = main(["watch", "run", "--all"])

    mock_generate.assert_not_called()
    assert result == 2
    assert calls == []
    assert "Auth error" in capsys.readouterr().err


def test_run_brief_step_unexpected_exception_returns_failure_code() -> None:
    """_run_brief_step must catch an unexpected exception from _run_generate_inner,
    log it, and return exit code 1 rather than propagating (historic regression class)."""
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.commands.brief.generate._run_generate_inner",
            side_effect=ValueError("LLM BadRequestError: model not servable"),
        ),
    ):
        rc, elapsed = _watch_cli_mod._run_brief_step(dry_run=False, no_llm=True)

    assert rc == WatcherRunResult("fatal", False, None)
    assert elapsed >= 0.0


def test_run_all_brief_rc_exceeding_watcher_codes_sets_max_code() -> None:
    """When the brief step's exit code is the largest of all results, it must
    still become the overall run --all exit code (max_code fold-in)."""
    name_to_rc = {
        "waiting-on-tracker": 0,
        "pursuit-stalls": 0,
        "close-date-countdown": 0,
        "contract-expiry": 0,
        "backstory-health": 0,
        "slack-threads": 0,
        "draft-queue": 0,
    }
    result = _run_all_run_run_all(name_to_rc, [], brief_rc=2)
    assert result.exit_code == 2


def test_run_with_all_and_name_is_usage_error() -> None:
    """`watch run --all NAME` is rejected — --all and a watcher name are mutually exclusive."""
    result = CliRunner().invoke(cli, ["run", "--all", "backstory-health"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_run_without_all_or_name_shows_help() -> None:
    """`watch run` with no NAME and no --all prints help and exits nonzero."""
    result = CliRunner().invoke(cli, ["run"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "Usage:" in result.output


def test_run_dispatches_to_named_watcher() -> None:
    """`watch run NAME` invokes the leaf watcher command with its own flags."""
    result = CliRunner().invoke(cli, ["run", "backstory-health", "--help"], catch_exceptions=False)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cron install logic
# ---------------------------------------------------------------------------


# ── TestInstallCron (flattened) ─────────────────────────────────────────────


def _install_cron_run(args: list[str]) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["run", "--all", *args], catch_exceptions=False)


def test_install_cron_install_cron_empty_crontab() -> None:
    """--install-cron on empty crontab (crontab -l exits 1) adds entry."""
    list_result = MagicMock()
    list_result.returncode = 1
    list_result.stdout = ""

    install_result = MagicMock()
    install_result.returncode = 0

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [list_result, install_result]
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 0
    assert mock_run.call_count == 2

    install_call = mock_run.call_args_list[1]
    assert install_call[0][0] == ["crontab", "-"]
    stdin_input: str = install_call[1]["input"]
    assert "fieldkit watch run --all" in stdin_input


def test_install_cron_install_cron_existing_entry_skips() -> None:
    """--install-cron deduplicates: if entry already present, no-op."""
    existing = "0 6 * * * fieldkit watch run --all\n"
    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = existing

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = list_result
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 0
    for c in mock_run.call_args_list:
        assert c[0][0] != ["crontab", "-"], "crontab - must not be called when entry exists"


def test_install_cron_install_cron_dry_run_prints_and_no_subprocess() -> None:
    """--install-cron --dry-run prints the entry but never calls subprocess."""
    with patch("subprocess.run") as mock_run:
        result = _install_cron_run(["--install-cron", "--dry-run"])

    assert result.exit_code == 0
    mock_run.assert_not_called()
    assert "fieldkit watch run --all" in result.output
    assert "dry-run" in result.output


def test_install_cron_install_cron_custom_time() -> None:
    """--cron-time uses custom expression in the installed line."""
    list_result = MagicMock()
    list_result.returncode = 1
    list_result.stdout = ""

    install_result = MagicMock()
    install_result.returncode = 0

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [list_result, install_result]
        result = _install_cron_run(["--install-cron", "--cron-time", "30 7 * * 1-5"])

    assert result.exit_code == 0
    install_call = mock_run.call_args_list[1]
    stdin_input: str = install_call[1]["input"]
    assert "30 7 * * 1-5" in stdin_input


def test_install_cron_install_cron_preserves_existing_entries() -> None:
    """--install-cron keeps existing crontab lines when adding new entry."""
    existing = "0 5 * * * some-other-command\n"
    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = existing

    install_result = MagicMock()
    install_result.returncode = 0

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [list_result, install_result]
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 0
    install_call = mock_run.call_args_list[1]
    stdin_input: str = install_call[1]["input"]
    assert "some-other-command" in stdin_input
    assert "fieldkit watch run --all" in stdin_input


def test_install_cron_install_cron_uses_absolute_binary_path() -> None:
    """--install-cron uses the absolute path returned by shutil.which, not 'fieldkit'."""
    abs_bin = "/usr/local/bin/fieldkit"

    list_result = MagicMock()
    list_result.returncode = 1
    list_result.stdout = ""

    install_result = MagicMock()
    install_result.returncode = 0

    with (
        patch("shutil.which", return_value=abs_bin),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.side_effect = [list_result, install_result]
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 0
    install_call = mock_run.call_args_list[1]
    stdin_input: str = install_call[1]["input"]
    assert abs_bin in stdin_input, f"Expected absolute path {abs_bin!r} in crontab, got: {stdin_input!r}"
    assert "fieldkit watch run --all" in stdin_input


def test_install_cron_reports_failed_crontab_write() -> None:
    with (
        patch("fieldkit.commands.watch.cli.shutil.which", return_value="/usr/local/bin/fieldkit"),
        patch(
            "fieldkit.commands.watch.cli.subprocess.run",
            side_effect=[
                CompletedProcess(["crontab", "-l"], 1, stdout=""),
                CompletedProcess(["crontab", "-"], 1, stderr="permission denied\n"),
            ],
        ) as mock_run,
    ):
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 1
    assert "Error installing crontab: permission denied" in result.stderr
    assert "Installed crontab entry:" not in result.stdout
    assert mock_run.call_count == 2
    install_call = mock_run.call_args_list[1]
    assert install_call.args == (["crontab", "-"],)
    assert "0 6 * * * /usr/local/bin/fieldkit watch run --all" in install_call.kwargs["input"]


def test_install_cron_install_cron_raises_when_binary_not_found() -> None:
    """--install-cron raises ClickException when shutil.which cannot find fieldkit."""
    with patch("shutil.which", return_value=None):
        result = _install_cron_run(["--install-cron"])

    assert result.exit_code == 1
    assert "fieldkit binary not found" in result.output.lower() or "not found in path" in result.output.lower()


def test_install_cron_install_cron_dry_run_uses_absolute_path() -> None:
    """--install-cron --dry-run prints absolute binary path, not bare 'fieldkit'."""
    abs_bin = "/usr/local/bin/fieldkit"

    with patch("shutil.which", return_value=abs_bin):
        result = _install_cron_run(["--install-cron", "--dry-run"])

    assert result.exit_code == 0
    assert abs_bin in result.output, f"Expected absolute path {abs_bin!r} in dry-run output: {result.output!r}"


# ---------------------------------------------------------------------------
# historic regression regression: watcher exceptions must NOT leak tracebacks to stdout
# ---------------------------------------------------------------------------


# ── TestExceptionDoesNotLeakToStdout (flattened) ────────────────────────────


def test_exception_does_not_leak_to_stdout_watcher_exception_stays_off_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ValueError raised by a watcher must NOT appear in run --all stdout (historic regression)."""
    calls: list[tuple[str, dict[str, object]]] = []
    outcomes: Mapping[str, object] = {**_all_zero(), "waiting-on-tracker": ValueError("watcher failure")}
    with (
        _patch_watcher_runs(outcomes, calls),
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.watch.integration_plan.build_integration_plan",
            return_value=IntegrationPlan((), ("backstory-health", "draft-queue", "slack-threads"), False, False, ()),
        ),
        patch("fieldkit.commands.watch.cli._run_brief_step", return_value=(_typed_result(0), 0.0)),
        pytest.raises(SystemExit, match="1"),
    ):
        _watch_cli_mod.run_all(dry_run=False, install_cron=False, cron_time="0 6 * * *", force=False)
    output = capsys.readouterr().out

    assert [name for name, _kwargs in calls] == _watch_cli_mod._WATCHER_ORDER
    assert "watcher failure" not in output, "Traceback text leaked to stdout (historic regression)"
    assert "ValueError" not in output
    assert "Traceback" not in output
