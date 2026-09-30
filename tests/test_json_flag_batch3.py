"""--json coverage tests for the third D3 rollout batch.

Batch 3 covers the sf/pursuit/meeting/ingest/init/sync/watch commands. As with
the issue and gmail batches, two properties are worth more than the individual
payload shapes, so they are asserted across every command rather than one by one:

  1. ``--json`` puts a single parseable document on *stdout*.
  2. ``--json`` selects a renderer. It never changes an exit code, and the
     default (flag absent) rendering stays prose.

The ``watch run`` watchers expose the run-status record they already compute
for ``watcher-run-status.json`` — the same schema, not a new one. Structured
outcomes remain distinct from process status; the Slack adapter maps its typed
domain result to the canonical success, partial, authentication, and invalid
exit codes.
"""

import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from fieldkit.config import ConfigError
from fieldkit.watch import backstory_health as backstory_domain
from fieldkit.watch import slack_threads as slack_domain
from fieldkit.watch.status import WatcherRunResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Every command in the batch declares the flag the same way
# ---------------------------------------------------------------------------

#: (module path, attribute) for each leaf command the batch added --json to.
_BATCH_COMMANDS = [
    pytest.param("fieldkit.commands.sf.frontmatter", "cli", id="sf-frontmatter"),
    pytest.param("fieldkit.commands.sf.meddpicc", "cli", id="sf-meddpicc"),
    pytest.param("fieldkit.commands.sf.reconcile", "cli", id="sf-reconcile"),
    pytest.param("fieldkit.commands.sf.set_field", "cli", id="sf-set-field"),
    pytest.param("fieldkit.commands.sf.set_next_steps", "cli", id="sf-set-next-steps"),
    pytest.param("fieldkit.commands.pursuit.advance_cmd", "advance_cmd", id="pursuit-advance"),
    pytest.param("fieldkit.commands.pursuit.archive_cmd", "cli", id="pursuit-archive"),
    pytest.param("fieldkit.commands.pursuit.rename_cmd", "cli", id="pursuit-rename"),
    pytest.param("fieldkit.commands.meeting.link_cmd", "cli", id="meeting-link"),
    pytest.param("fieldkit.commands.meeting.list_cmd", "cli", id="meeting-list"),
    pytest.param("fieldkit.commands.meeting.note_cmd", "cli", id="meeting-note"),
    pytest.param("fieldkit.commands.meeting.open_cmd", "cli", id="meeting-open"),
    pytest.param("fieldkit.commands.ingest.backfill", "cli", id="ingest-backfill"),
    pytest.param("fieldkit.commands.ingest.discover", "cli", id="ingest-discover"),
    pytest.param("fieldkit.commands.ingest.route", "cli", id="ingest-route"),
    pytest.param("fieldkit.commands.ingest.status", "cli", id="ingest-status"),
    pytest.param("fieldkit.commands.datasync.cli", "cli", id="sync"),
    pytest.param("fieldkit.commands.watch.cli", "status_cmd", id="watch-status"),
    pytest.param("fieldkit.commands.watch.pursuit_stalls", "ack_cmd", id="watch-pursuit-stalls-ack"),
    pytest.param("fieldkit.commands.watch.backstory_health", "cli", id="watch-backstory-health"),
    pytest.param("fieldkit.commands.watch.close_date_countdown", "cli", id="watch-close-date-countdown"),
    pytest.param("fieldkit.commands.watch.contract_expiry", "cli", id="watch-contract-expiry"),
    pytest.param("fieldkit.commands.watch.draft_queue", "cli", id="watch-draft-queue"),
    pytest.param("fieldkit.commands.watch.slack_threads", "cli", id="watch-slack-threads"),
    pytest.param("fieldkit.commands.watch.waiting_on_tracker", "cli", id="watch-waiting-on-tracker"),
]


def _load_command(module_path: str, attr: str) -> click.Command:
    command = getattr(importlib.import_module(module_path), attr)
    assert isinstance(command, click.Command)
    return command


@pytest.mark.parametrize(("module_path", "attr"), _BATCH_COMMANDS)
def test_command_declares_json_as_an_opt_in_flag(module_path: str, attr: str) -> None:
    """One shape for the flag everywhere: ``--json`` → ``as_json``, off by default.

    A command that spelled the destination differently would still satisfy the
    coverage counter while breaking every caller that passes ``as_json=``.
    """
    command = _load_command(module_path, attr)

    json_params = [p for p in command.params if "--json" in getattr(p, "opts", [])]
    assert len(json_params) == 1, f"{module_path}.{attr} declares {len(json_params)} --json params"
    param = json_params[0]
    assert isinstance(param, click.Option)
    assert param.name == "as_json"
    assert param.is_flag
    assert param.default is False


# ---------------------------------------------------------------------------
# 2. The six watch-run watchers: the run-status record is the document
# ---------------------------------------------------------------------------

#: Keys every watcher document carries — the write_run_status schema.
_RUN_STATUS_KEYS = {
    "watcher",
    "outcome",
    "records_checked",
    "alerts_generated",
    "failures",
    "elapsed_seconds",
    "dry_run",
}


def _run_backstory(as_json: bool, *, checked: int = 2, failures: int = 0) -> int:
    mod = importlib.import_module("fieldkit.watch.backstory_health")
    with (
        patch.object(mod, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(mod, "get_mcp_endpoint", return_value="https://gateway.example.com/backstory"),
        patch.object(mod, "_open_mcp_session", return_value=MagicMock()),
        patch.object(mod, "load_state", return_value={}),
        patch.object(mod, "save_state"),
        patch.object(mod, "log_run_summary"),
        patch.object(
            mod,
            "_check_all_accounts",
            return_value=backstory_domain._AccountScanResult(checked + failures, checked, 1, failures, 0, {}),
        ),
        patch.object(mod, "write_run_status", return_value="written"),
    ):
        result = mod._run_backstory_health(threshold=3, account=None, dry_run=False, as_json=as_json)
        assert isinstance(result, WatcherRunResult)
        assert result.completed is True
        assert result.status_write == "written"
        return result.exit_code


def _run_countdown(as_json: bool, *, checked: int = 3, skipped: int = 0) -> int:
    mod = importlib.import_module("fieldkit.watch.close_date_countdown")
    with (
        patch.object(mod, "_validate_accounts_config", return_value=({"acme": {}}, 0)),
        patch.object(mod, "_prune_and_scan_pursuits", return_value=(checked, 1, skipped, 0, {})),
        patch.object(mod, "_save_state"),
        patch.object(mod, "write_run_status", return_value="written"),
    ):
        result = mod._run_countdown(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
            as_json=as_json,
        )
        assert isinstance(result, WatcherRunResult)
        assert result.completed is True
        assert result.status_write == "written"
        return result.exit_code


def _run_contract_expiry(as_json: bool, *, tmp_path: Path) -> int:
    mod = importlib.import_module("fieldkit.watch.contract_expiry")
    (tmp_path / "accounts").mkdir(parents=True, exist_ok=True)
    with (
        patch.object(mod, "get_fieldkit_home", return_value=tmp_path),
        patch.object(mod, "_load_state", return_value={}),
        patch.object(mod, "_save_state"),
    ):
        result = mod._run_contract_expiry(account_filter=None, dry_run=False, as_json=as_json)
        assert isinstance(result, WatcherRunResult)
        assert result.completed is True
        assert result.status_write == "written"
        return result.exit_code


def _run_draft_queue(as_json: bool) -> int:
    mod = importlib.import_module("fieldkit.watch.draft_queue")
    with (
        patch.object(mod, "write_alerts"),
        patch.object(mod, "write_run_status", return_value="skipped") as status_writer,
    ):
        result = mod._run_draft_queue(dry_run=True, as_json=as_json)
        assert result == WatcherRunResult("ok", True, "skipped")
        assert status_writer.call_args.kwargs["dry_run"] is True
        return result.exit_code


def _run_slack_threads(as_json: bool, *, checked: int = 2, auth_error: bool = False) -> int:
    with (
        patch.object(slack_domain, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(slack_domain, "load_current_username", return_value="tester"),
        patch.object(slack_domain, "load_state", return_value={}),
        patch.object(
            slack_domain,
            "_scan_all_accounts",
            return_value=(checked, 1, "auth" if auth_error else None, 0),
        ),
        patch.object(slack_domain, "_persist_and_summarise", return_value=0),
        patch.object(slack_domain, "write_run_status", return_value="written"),
    ):
        args = ["--json"] if as_json else []
        result = CliRunner().invoke(importlib.import_module("fieldkit.commands.watch.slack_threads").cli, args)
        click.echo(result.stdout, nl=False)
        return result.exit_code


def _run_waiting_on(as_json: bool, *, content: str | None = "## Waiting On\n") -> int:
    mod = importlib.import_module("fieldkit.watch.waiting_on_tracker")
    with (
        patch.object(mod, "_read_tasks_md", return_value=content),
        patch.object(mod, "_load_state", return_value={}),
        patch.object(mod, "_save_state"),
    ):
        result = mod._run(threshold=7, dry_run=False, as_json=as_json)
        assert isinstance(result, WatcherRunResult)
        assert result.completed is True
        assert result.status_write == "written"
        return result.exit_code


#: (driver, watcher name) for each watcher whose run emits a run-status document.
_WATCHERS = [
    pytest.param(_run_backstory, "backstory-health", id="backstory-health"),
    pytest.param(_run_countdown, "close-date-countdown", id="close-date-countdown"),
    pytest.param(_run_draft_queue, "draft-queue", id="draft-queue"),
    pytest.param(_run_slack_threads, "slack-threads", id="slack-threads"),
    pytest.param(_run_waiting_on, "waiting-on-tracker", id="waiting-on-tracker"),
]


@pytest.mark.parametrize(("driver", "watcher"), _WATCHERS)
def test_watcher_json_emits_the_run_status_document(
    driver: Any, watcher: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = driver(True)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= _RUN_STATUS_KEYS
    assert payload["watcher"] == watcher


@pytest.mark.parametrize(("driver", "watcher"), _WATCHERS)
def test_watcher_without_json_writes_no_document(driver: Any, watcher: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The default rendering is untouched — --json is opt-in, never the default."""
    exit_code = driver(False)

    assert exit_code == 0
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        json.loads(out)


def test_contract_expiry_json_emits_the_run_status_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = _run_contract_expiry(True, tmp_path=tmp_path)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= _RUN_STATUS_KEYS
    assert payload["watcher"] == "contract-expiry"
    assert payload["outcome"] == "ok"


# --- Slack domain results stay typed; the adapter owns process exit codes ----------------


def test_slack_threads_partial_auth_outcome_returns_auth_result() -> None:
    """An auth failure retains AUTH even when some account reads completed."""
    with (
        patch.object(slack_domain, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(slack_domain, "load_current_username", return_value="tester"),
        patch.object(slack_domain, "load_state", return_value={}),
        patch.object(slack_domain, "_scan_all_accounts", return_value=(2, 1, "auth", 0)),
        patch.object(slack_domain, "_persist_and_summarise", return_value=0),
        patch.object(slack_domain, "write_run_status", return_value="written"),
    ):
        outcome = slack_domain._run_slack_threads(
            threshold_hours=24,
            account=None,
            limit=50,
            dry_run=False,
        )

    assert outcome.run.exit_code == 2
    assert outcome.run.completed is False
    assert outcome.run.status_write == "written"
    assert outcome.run.outcome == "partial"
    assert outcome.records_checked == 2
    assert outcome.auth_error is True


def test_slack_threads_fatal_auth_outcome_returns_auth_result() -> None:
    """Zero completed reads is a fatal document outcome and an AUTH domain result."""
    with (
        patch.object(slack_domain, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(slack_domain, "load_current_username", return_value="tester"),
        patch.object(slack_domain, "load_state", return_value={}),
        patch.object(slack_domain, "_scan_all_accounts", return_value=(0, 1, "auth", 0)),
        patch.object(slack_domain, "_persist_and_summarise", return_value=0),
        patch.object(slack_domain, "write_run_status", return_value="written"),
    ):
        outcome = slack_domain._run_slack_threads(
            threshold_hours=24,
            account=None,
            limit=50,
            dry_run=False,
        )

    assert outcome.run.exit_code == 2
    assert outcome.run.completed is False
    assert outcome.run.status_write == "written"
    assert outcome.run.outcome == "fatal"


def test_slack_threads_adapter_maps_auth_result_to_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = _run_slack_threads(True, checked=1, auth_error=True)

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["auth_error"] is True


def test_backstory_health_partial_outcome_is_reported_verbatim(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(backstory_domain, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(backstory_domain, "get_mcp_endpoint", return_value="https://gateway.example.com/backstory"),
        patch.object(backstory_domain, "_open_mcp_session", return_value=MagicMock()),
        patch.object(backstory_domain, "load_state", return_value={}),
        patch.object(backstory_domain, "save_state"),
        patch.object(backstory_domain, "log_run_summary"),
        patch.object(
            backstory_domain, "_check_all_accounts", return_value=backstory_domain._AccountScanResult(3, 2, 1, 1, 0, {})
        ),
        patch.object(backstory_domain, "write_run_status", return_value="written"),
    ):
        capsys.readouterr()
        result = backstory_domain._run_backstory_health(threshold=3, account=None, dry_run=False, as_json=True)
        stdout = capsys.readouterr().out

    assert result == WatcherRunResult("partial", True, "written")
    assert '"watcher": "backstory-health"' in stdout
    payload = json.loads(stdout)
    assert payload["outcome"] == "partial"
    assert payload["failures"] == 1


def test_backstory_health_fatal_when_nothing_was_checked(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(backstory_domain, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(backstory_domain, "get_mcp_endpoint", return_value="https://gateway.example.com/backstory"),
        patch.object(backstory_domain, "_open_mcp_session", return_value=MagicMock()),
        patch.object(backstory_domain, "load_state", return_value={}),
        patch.object(backstory_domain, "save_state"),
        patch.object(backstory_domain, "log_run_summary"),
        patch.object(
            backstory_domain, "_check_all_accounts", return_value=backstory_domain._AccountScanResult(1, 0, 0, 1, 0, {})
        ),
        patch.object(backstory_domain, "write_run_status", return_value="written"),
    ):
        capsys.readouterr()
        result = backstory_domain._run_backstory_health(threshold=3, account=None, dry_run=False, as_json=True)
        stdout = capsys.readouterr().out

    assert result == WatcherRunResult("fatal", True, "written")
    assert '"watcher": "backstory-health"' in stdout
    payload = json.loads(stdout)
    assert payload["outcome"] == "fatal"
    assert payload["records_checked"] == 0


def test_draft_queue_missing_email_is_invalid_data_with_json_requested() -> None:
    """JSON selection does not turn invalid configuration into a run result."""
    mod = importlib.import_module("fieldkit.watch.draft_queue")
    with (
        patch.object(mod, "get_mcp_endpoint", return_value="https://gateway.example.com/drafts"),
        patch.object(mod, "_resolve_user_email", return_value=""),
        pytest.raises(ConfigError, match="requires a user email"),
    ):
        mod._run_draft_queue(dry_run=False, as_json=True)


def test_draft_queue_missing_email_is_invalid_data_without_json() -> None:
    mod = importlib.import_module("fieldkit.watch.draft_queue")
    with (
        patch.object(mod, "get_mcp_endpoint", return_value="https://gateway.example.com/drafts"),
        patch.object(mod, "_resolve_user_email", return_value=""),
        pytest.raises(ConfigError, match="requires a user email"),
    ):
        mod._run_draft_queue(dry_run=False, as_json=False)


def test_waiting_on_missing_tasks_file_still_emits_a_document(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 0 with empty stdout would read as "no result" to a caller that
    parses on success; the run happened and checked zero items."""
    exit_code = _run_waiting_on(True, content=None)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ok"
    assert payload["records_checked"] == 0


# ---------------------------------------------------------------------------
# 3. watch status / pursuit-stalls ack
# ---------------------------------------------------------------------------


def test_watch_status_json_is_a_list_shaped_document() -> None:
    statuses = {
        "slack-threads": {"outcome": "partial", "last_run": "2026-07-01T06:45:01Z"},
        "draft-queue": {"outcome": "ok", "last_run": "2026-07-01T06:40:00Z"},
    }
    watch_cli = importlib.import_module("fieldkit.commands.watch.cli")
    with patch.object(watch_cli._watch_status, "load_all_statuses", return_value=statuses):
        result = CliRunner().invoke(watch_cli.status_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"items", "count", "filters"}
    assert payload["count"] == 2
    # Sorted by watcher name, and `partial` is carried through unflattened.
    assert [i["watcher"] for i in payload["items"]] == ["draft-queue", "slack-threads"]
    assert payload["items"][1]["outcome"] == "partial"


def test_watch_status_json_empty_is_a_document_not_a_prose_hint() -> None:
    watch_cli = importlib.import_module("fieldkit.commands.watch.cli")
    with patch.object(watch_cli._watch_status, "load_all_statuses", return_value={}):
        result = CliRunner().invoke(watch_cli.status_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"items": [], "count": 0, "filters": {}}


def test_pursuit_stalls_ack_json_reports_the_snooze_window() -> None:
    stalls = importlib.import_module("fieldkit.commands.watch.pursuit_stalls")
    with patch.object(stalls, "snooze_pursuit") as snooze:
        result = CliRunner().invoke(stalls.ack_cmd, ["acme-corp/renewal", "--days", "3", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pursuit"] == "acme-corp/renewal"
    assert payload["days"] == 3
    assert payload["snoozed"] is True
    snooze.assert_called_once_with("acme-corp/renewal", days=3)


def test_pursuit_stalls_ack_without_json_stays_prose() -> None:
    stalls = importlib.import_module("fieldkit.commands.watch.pursuit_stalls")
    with patch.object(stalls, "snooze_pursuit"):
        result = CliRunner().invoke(stalls.ack_cmd, ["acme-corp/renewal", "--json"])
        prose = CliRunner().invoke(stalls.ack_cmd, ["acme-corp/renewal"])

    assert json.loads(result.stdout)["pursuit"] == "acme-corp/renewal"
    assert prose.stdout.startswith("Snoozed acme-corp/renewal until ")


def test_pursuit_stalls_ack_state_write_failure_leaves_stdout_empty() -> None:
    """No result exists yet, so stdout stays empty and the exit code is unchanged."""
    stalls = importlib.import_module("fieldkit.commands.watch.pursuit_stalls")
    with patch.object(stalls, "snooze_pursuit", side_effect=OSError("disk full")):
        result = CliRunner().invoke(stalls.ack_cmd, ["acme-corp/renewal", "--json"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "disk full" in result.stderr


# ---------------------------------------------------------------------------
# 4. The meeting / sf / pursuit / ingest / init commands at the CLI surface
# ---------------------------------------------------------------------------


def _pursuit_file(tmp_path: Path, *, stage: str = "discover", account: str = "acme-corp") -> Path:
    """A pursuit file with historical local scores for containment coverage."""
    pursuits = tmp_path / "accounts" / account / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    path = pursuits / "renewal.md"
    scores = "\n".join(
        f"  {k}: 3"
        for k in (
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "paper-process",
            "identify-pain",
            "champion",
            "competition",
        )
    )
    path.write_text(
        f"---\nsf_opportunity_id: 006AAA00000000AAA\nstage: {stage}\ngate-status: pending\nmeddpicc:\n{scores}\n---\n\n# Pursuit\n",
        encoding="utf-8",
    )
    return path


def test_meeting_list_json_is_list_shaped(tmp_path: Path) -> None:
    list_cmd = importlib.import_module("fieldkit.commands.meeting.list_cmd")
    entries = [
        MagicMock(relative_path=Path("accounts/acme/pursuits/a.md"), url="https://docs/a"),
        MagicMock(relative_path=Path("accounts/acme/pursuits/b.md"), url="https://docs/b"),
    ]
    with (
        patch.object(list_cmd, "get_fieldkit_home", return_value=tmp_path),
        patch.object(list_cmd, "list_meetings", return_value=entries),
    ):
        result = CliRunner().invoke(list_cmd.cli, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"items", "count", "filters"}
    assert payload["count"] == 2
    assert payload["filters"] == {"account": None}
    assert [i["url"] for i in payload["items"]] == ["https://docs/a", "https://docs/b"]


def test_meeting_open_json_reports_the_url_and_still_opens(tmp_path: Path) -> None:
    open_cmd = importlib.import_module("fieldkit.commands.meeting.open_cmd")
    target = _pursuit_file(tmp_path)
    with (
        patch.object(open_cmd, "open_doc", return_value="https://docs/x"),
        patch.object(open_cmd.webbrowser, "open") as browser,
    ):
        result = CliRunner().invoke(open_cmd.cli, [str(target), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"] == "https://docs/x"
    assert payload["opened"] is True
    browser.assert_called_once_with("https://docs/x")


def test_meeting_link_json_distinguishes_created_from_already_linked(tmp_path: Path) -> None:
    link_cmd = importlib.import_module("fieldkit.commands.meeting.link_cmd")
    target = _pursuit_file(tmp_path)

    with patch.object(link_cmd, "link", return_value=MagicMock(url="https://docs/w", already_linked=False)):
        created = CliRunner().invoke(link_cmd.cli, [str(target), "--json"])
    with patch.object(link_cmd, "link", return_value=MagicMock(url="https://docs/w", already_linked=True)):
        existing = CliRunner().invoke(link_cmd.cli, [str(target), "--json"])

    assert created.exit_code == 0, created.output
    created_payload = json.loads(created.stdout)
    assert created_payload["created"] is True
    assert created_payload["already_linked"] is False
    assert json.loads(existing.stdout)["created"] is False


def test_meeting_link_already_linked_does_not_reopen_the_browser(tmp_path: Path) -> None:
    """Pre-existing behavior: an already-linked workbook is not re-opened, and
    --json must not have quietly changed that by removing the early return."""
    link_cmd = importlib.import_module("fieldkit.commands.meeting.link_cmd")
    target = _pursuit_file(tmp_path)
    with (
        patch.object(link_cmd, "link", return_value=MagicMock(url="https://docs/w", already_linked=True)),
        patch.object(link_cmd.webbrowser, "open") as browser,
    ):
        result = CliRunner().invoke(link_cmd.cli, [str(target), "--open"])

    assert result.exit_code == 0, result.output
    browser.assert_not_called()


def test_meeting_note_json_names_the_added_tab(tmp_path: Path) -> None:
    note_cmd = importlib.import_module("fieldkit.commands.meeting.note_cmd")
    target = _pursuit_file(tmp_path)
    with patch.object(note_cmd, "add_note", return_value=MagicMock(tab_name="2026-07-27 QBR", url="https://docs/w")):
        result = CliRunner().invoke(note_cmd.cli, [str(target), "--title", "QBR", "--content", "notes", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tab_name"] == "2026-07-27 QBR"
    assert payload["added"] is True


def test_sf_reconcile_json_reports_status_and_preserves_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconcile = importlib.import_module("fieldkit.commands.sf.reconcile")
    path = tmp_path / "accounts" / "acme-corp" / "pursuits" / "renewal.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nstage: discover\ngate-status: pending\nsf_stage: Propose\n---\n"
        "\n## Key Fields\n\n| Field | Value |\n| ----- | ----- |\n| Stage | Discover |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(reconcile, "get_fieldkit_home", lambda: tmp_path)
    ok = CliRunner().invoke(reconcile.cli, [str(path), "--json"])
    failed = CliRunner().invoke(reconcile.cli, [str(path.with_name("missing.md")), "--json"])

    assert ok.exit_code == 0, ok.output
    ok_payload = json.loads(ok.stdout)
    assert ok_payload["status"] == "updated"
    assert ok_payload["changed"] is True

    # Missing or invalid targets share the data-error taxonomy in dry and live modes.
    assert failed.exit_code == 3
    assert json.loads(failed.stdout)["status"] == "error"


def test_sf_meddpicc_json_flags_a_missing_closeplan() -> None:
    meddpicc = importlib.import_module("fieldkit.commands.sf.meddpicc")
    scorecard = {
        "org_url": "https://example.my.salesforce.com",
        "opportunity_id": "006AAA00000000AAAA",
        "status": "not_found",
        "complete": True,
        "selected_deal_id": None,
        "deals_reported_count": 0,
        "deals": [],
        "field_metadata": {},
        "metadata_gaps": [],
        "issues": [],
    }
    with (
        patch.object(meddpicc, "get_sf_session_id", return_value="sid"),
        patch.object(meddpicc, "fetch_meddpicc_scorecard", return_value=scorecard),
    ):
        result = CliRunner().invoke(meddpicc.cli, ["006AAA00000000AAAA", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "not_found"
    assert payload["complete"] is True
    assert payload["deals"] == []


def test_sf_set_field_list_fields_json_carries_the_allowlist() -> None:
    set_field = importlib.import_module("fieldkit.commands.sf.set_field")
    result = CliRunner().invoke(set_field.cli, ["--list-fields", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["allowed_fields"] == set_field.ALLOWED_FIELDS


def test_sf_set_next_steps_preview_json_does_not_write() -> None:
    """Without --confirm the command previews; --json reports that as an outcome
    rather than staying silent."""
    sns = importlib.import_module("fieldkit.commands.sf.set_next_steps")
    client = MagicMock()
    client.__enter__.return_value = client
    with (
        patch.object(sns, "get_sf_session_id", return_value="sid"),
        patch.object(sns, "get_sf_rest_base_url", return_value="https://sf/"),
        patch.object(sns, "SFDirectClient", return_value=client),
        patch.object(sns, "_fetch_opportunity", return_value={"Name": "Acme", "Next_Steps__c": "old"}, create=True),
    ):
        result = CliRunner().invoke(sns.cli, ["006AAA00000000AAA", "new plan", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "preview"
    assert payload["written"] is False


def test_pursuit_archive_json_reports_each_file(tmp_path: Path) -> None:
    archive = importlib.import_module("fieldkit.commands.pursuit.archive_cmd")
    _pursuit_file(tmp_path, stage="closed-won")
    with patch.object(archive, "_data_root", return_value=tmp_path):
        result = CliRunner().invoke(archive.cli, ["--account", "acme-corp", "--all-closed", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["account"] == "acme-corp"
    assert payload["archived"] == 1
    assert payload["items"][0]["outcome"] == "archived"
    assert (tmp_path / "accounts" / "acme-corp" / "archive" / "renewal.md").exists()


def test_pursuit_archive_dry_run_json_moves_nothing(tmp_path: Path) -> None:
    archive = importlib.import_module("fieldkit.commands.pursuit.archive_cmd")
    source = _pursuit_file(tmp_path, stage="closed-lost")
    with patch.object(archive, "_data_root", return_value=tmp_path):
        result = CliRunner().invoke(archive.cli, ["--account", "acme-corp", "--all-closed", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["items"][0]["outcome"] == "would-archive"
    assert source.exists()


def test_pursuit_rename_json_lists_the_state_files_touched(tmp_path: Path) -> None:
    rename = importlib.import_module("fieldkit.commands.pursuit.rename_cmd")
    _pursuit_file(tmp_path)
    watchers = tmp_path / "watchers"
    watchers.mkdir(parents=True, exist_ok=True)
    (watchers / "pursuit-stall-state.json").write_text(
        json.dumps({"acme-corp/renewal": {"pursuit": "renewal"}}), encoding="utf-8"
    )
    with patch.object(rename, "get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(
            rename.cli, ["--account", "acme-corp", "--from", "renewal", "--to", "expansion", "--json"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["renamed"] is True
    assert payload["state_updates"] == ["pursuit-stall-state.json"]
    assert payload["new_key"] == "acme-corp/expansion"
    assert (tmp_path / "accounts" / "acme-corp" / "pursuits" / "expansion.md").exists()


def test_ingest_backfill_json_is_list_shaped(tmp_path: Path) -> None:
    backfill = importlib.import_module("fieldkit.commands.ingest.backfill")
    meetings = tmp_path / "accounts" / "acme-corp" / "meetings"
    meetings.mkdir(parents=True)
    (meetings / "2026-07-01-qbr.md").write_text("---\ntitle: QBR\n---\n\nbody\n", encoding="utf-8")
    with patch.object(backfill, "get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(backfill.cli, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) >= {"items", "count", "scanned", "filters"}
    assert payload["scanned"] == 1
    assert payload["count"] == len(payload["items"])


def test_ingest_route_json_reports_an_absent_unknown_dir(tmp_path: Path) -> None:
    """Exit 0 with prose-only stdout would read as "no result" to a caller."""
    route = importlib.import_module("fieldkit.commands.ingest.route")
    result = CliRunner().invoke(route.cli, ["--data-root", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "scan"
    assert payload["considered"] == 0
    assert payload["note"] is not None


def test_ingest_status_json_is_list_shaped() -> None:
    status = importlib.import_module("fieldkit.commands.ingest.status")
    result = CliRunner().invoke(status.cli, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) >= {"items", "count", "db_connected", "filters"}
    assert payload["count"] == len(payload["items"])
    assert all("pipeline_id" in item for item in payload["items"])


def test_sf_frontmatter_validate_json_carries_the_errors(tmp_path: Path) -> None:
    frontmatter = importlib.import_module("fieldkit.commands.sf.frontmatter")
    target = _pursuit_file(tmp_path)
    with patch.object(frontmatter, "validate_pursuit_content", return_value=("stage: unknown value",)):
        result = CliRunner().invoke(frontmatter.cli, ["--validate", "--file", str(target), "--json"])

    # The validation ran and denied — exit 1 is unchanged and the errors ship.
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid"
    assert payload["errors"]


def test_sf_frontmatter_quality_check_json_counts_advisories(tmp_path: Path) -> None:
    frontmatter = importlib.import_module("fieldkit.commands.sf.frontmatter")
    target = _pursuit_file(tmp_path)
    result = CliRunner().invoke(frontmatter.cli, ["--quality-check", "--file", str(target), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "quality-check"
    assert payload["has_frontmatter"] is True
    assert isinstance(payload["advisory_count"], int)


def test_pursuit_advance_json_reports_a_pending_native_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    advance = importlib.import_module("fieldkit.commands.pursuit.advance_cmd")
    monkeypatch.setattr(advance, "get_accounts_root", lambda: tmp_path / "accounts")
    target = _pursuit_file(tmp_path)
    result = CliRunner().invoke(advance.advance_cmd, [str(target), "--dry-run", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["gate_passed"] is False
    assert payload["gate_status"] == "pending"
    assert payload["reasons"]
    assert payload["advanced"] is False
    assert payload["from_stage"] == "discover"


def test_pursuit_advance_json_carries_reasons_when_policy_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending gate is exactly when the caller needs the reason list, so the
    document ships alongside the unchanged exit 1."""
    advance = importlib.import_module("fieldkit.commands.pursuit.advance_cmd")
    monkeypatch.setattr(advance, "get_accounts_root", lambda: tmp_path / "accounts")
    pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    pursuits.mkdir(parents=True)
    target = pursuits / "renewal.md"
    target.write_text(
        "---\nstage: discover\nmeddpicc:\n  metrics: 0\n  champion: 0\n---\n\n# Pursuit\n", encoding="utf-8"
    )
    result = CliRunner().invoke(advance.advance_cmd, [str(target), "--dry-run", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["gate_passed"] is False
    assert payload["gate_status"] == "pending"
    assert payload["reasons"]


def test_sync_json_reports_every_step_and_preserves_the_failure_exit() -> None:
    datasync = importlib.import_module("fieldkit.commands.datasync.cli")
    results = [
        datasync.StepResult(index=1, total=2, label="gmail sync", cmd=["gmail"], success=True, elapsed=1.0),
        datasync.StepResult(index=2, total=2, label="ingest", cmd=["ingest"], success=False, elapsed=2.0),
    ]
    with (
        patch.object(datasync, "run_pipeline", return_value=results),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = CliRunner().invoke(datasync.cli, ["--json"])

    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["failed"] == 1
    assert payload["succeeded"] == 1
    # A failed step still exits 1 — --json changed the format, not the outcome.
    assert result.exit_code == 1
