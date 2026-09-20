"""--json contract tests for the gmail, driver, health, shadowbot, companion, web commands.

Every command here gained ``--json`` in the D3 rule-3 rollout. The contract each
test holds the commands to is the same one the ratchet in
``scripts/check_flag_contract.py`` exists to protect:

  * stdout is a single JSON document — nothing else is interleaved into it;
  * the exit code is unchanged by the flag, so ``--json`` can be added to an
    existing invocation without breaking the caller's error handling;
  * omitting the flag leaves the prose output exactly as it was.

The third point is the one worth testing explicitly: a rollout that quietly
reshapes default output breaks every operator's eyes and every script that
greps it, and the JSON tests alone would not notice.
"""

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.companion.cli import cli as companion_cli
from fieldkit.commands.driver.cli import cli as driver_cli
from fieldkit.commands.gmail.account_tags import cli as account_tags_cli
from fieldkit.commands.gmail.query import cli as query_cli
from fieldkit.commands.health.cli import cli as health_cli
from fieldkit.commands.shadowbot.cli import cli as shadowbot_cli
from fieldkit.commands.web.cli import cli as web_cli
from fieldkit.health.runner import HealthRunResult
from fieldkit.shadowbot.client import ShadowbotResponse

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Gmail fixtures — an in-memory gmail.db with one person, thread, and tag
# ---------------------------------------------------------------------------


def _make_gmail_db() -> sqlite3.Connection:
    """Create a minimal in-memory gmail.db carrying one tagged thread."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE messages (
            msg_id TEXT PRIMARY KEY,
            thread_id TEXT,
            subject TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            body_plain TEXT
        );
        CREATE TABLE people (
            person_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE thread_accounts (
            thread_id TEXT,
            account TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO people (person_id, email, display_name) VALUES (?, ?, ?)",
        ("alice@acme-corp.com", "alice@acme-corp.com", "Alice Smith"),
    )
    conn.execute(
        "INSERT INTO messages (msg_id, thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch,"
        " body_plain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            "t-001",
            "Renewal pricing",
            "alice@acme-corp.com",
            "rep@example.com",
            "",
            "2023-11-14",
            1700000000,
            "Here is the renewal pricing you asked for.\n> quoted reply",
        ),
    )
    conn.execute(
        "INSERT INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, 1, ?)",
        ("t-001", "Renewal pricing", "2023-11-14"),
    )
    conn.execute("INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)", ("t-001", "acme-corp"))
    conn.commit()
    return conn


# The 7 `gmail query` subcommands are near-identical adapters over one shared
# options decorator, so they are parametrized rather than copy-pasted. champion
# is the odd one out: the domain hands back a rendered report, not a record set.
_QUERY_INVOCATIONS = [
    ("person", ["person", "Alice Smith"], "items"),
    ("context", ["context", "Alice Smith"], "items"),
    ("account", ["account", "acme-corp"], "items"),
    ("dig", ["dig", "acme-corp", "pricing"], "items"),
    ("threads", ["threads", "Renewal"], "items"),
    ("champion", ["champion", "Alice Smith"], "report"),
    ("blindspots", ["blindspots", "acme-corp"], "items"),
]


@pytest.mark.parametrize(("name", "args", "key"), _QUERY_INVOCATIONS, ids=[i[0] for i in _QUERY_INVOCATIONS])
def test_gmail_query_json_emits_one_document(name: str, args: list[str], key: str, tmp_path: Path) -> None:
    """Every `gmail query` subcommand emits a single parseable document under --json."""
    db = _make_gmail_db()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = CliRunner().invoke(query_cli, [*args, "--db", str(tmp_path / "fake.db"), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert key in payload
    assert "filters" in payload


@pytest.mark.parametrize(("name", "args", "key"), _QUERY_INVOCATIONS, ids=[i[0] for i in _QUERY_INVOCATIONS])
def test_gmail_query_json_absent_emits_no_json(name: str, args: list[str], key: str, tmp_path: Path) -> None:
    """Without --json the subcommands still render prose, not a document."""
    db = _make_gmail_db()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = CliRunner().invoke(query_cli, [*args, "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        json.loads(result.stdout)


def test_gmail_query_person_json_no_match_is_an_empty_document(tmp_path: Path) -> None:
    """A name that matches nobody yields count 0, not a bare prose line."""
    db = _make_gmail_db()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = CliRunner().invoke(query_cli, ["person", "Nobody At All", "--db", str(tmp_path / "x.db"), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["items"] == []
    assert payload["filters"]["name"] == "Nobody At All"


def test_gmail_query_filters_carry_the_parsed_epoch(tmp_path: Path) -> None:
    """--since reaches the document as the epoch the query actually ran with."""
    db = _make_gmail_db()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = CliRunner().invoke(
            query_cli,
            ["threads", "Renewal", "--db", str(tmp_path / "x.db"), "--since", "2023-01-01", "--json"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["filters"]["since"] == 1672531200
    assert payload["filters"]["before"] is None


# ---------------------------------------------------------------------------
# gmail account-tags
# ---------------------------------------------------------------------------


def _tagging_db(tmp_path: Path) -> Path:
    """Write a gmail.db on disk with one ref/* labelled message."""
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (msg_id TEXT PRIMARY KEY, thread_id TEXT, labels TEXT);
        CREATE TABLE labels (label_id TEXT PRIMARY KEY, label_name TEXT);
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT, PRIMARY KEY (thread_id, account));
        """
    )
    conn.execute("INSERT INTO labels (label_id, label_name) VALUES ('L1', 'ref/acme-corp')")
    conn.execute("INSERT INTO messages (msg_id, thread_id, labels) VALUES ('m1', 't-001', '[\"L1\"]')")
    conn.commit()
    conn.close()
    return db_path


def test_gmail_account_tags_json_summarizes_the_upsert(tmp_path: Path) -> None:
    """account-tags reports per-account counts as a document under --json."""
    db_path = _tagging_db(tmp_path)

    result = CliRunner().invoke(account_tags_cli, ["--db", str(db_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["items"] == [{"account": "acme-corp", "threads": 1}]
    assert payload["count"] == 1
    assert payload["associations"] == 1


def test_gmail_account_tags_prose_is_unchanged(tmp_path: Path) -> None:
    """Without --json the operator still gets the upsert summary lines."""
    db_path = _tagging_db(tmp_path)

    result = CliRunner().invoke(account_tags_cli, ["--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "Upserted 1 thread-account associations across 1 account(s):" in result.output
    assert "  acme-corp: 1 threads" in result.output


def test_gmail_account_tags_json_with_no_labels_is_an_empty_document(tmp_path: Path) -> None:
    """No ref/* labels yields an empty document rather than the prose notice."""
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (msg_id TEXT PRIMARY KEY, thread_id TEXT, labels TEXT);
        CREATE TABLE labels (label_id TEXT PRIMARY KEY, label_name TEXT);
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT, PRIMARY KEY (thread_id, account));
        """
    )
    conn.commit()
    conn.close()

    result = CliRunner().invoke(account_tags_cli, ["--db", str(db_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["associations"] == 0


# ---------------------------------------------------------------------------
# gmail backstory-gap
# ---------------------------------------------------------------------------


def _backstory_env(tmp_path: Path) -> tuple[Path, Path]:
    """Build an accounts.yaml and a people-bearing gmail.db under *tmp_path*."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "accounts.yaml").write_text(
        "accounts:\n  acme-corp:\n    domains: [acme-corp.com]\n    blindspots_min_messages: 2\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            message_count INTEGER,
            thread_count INTEGER,
            meeting_count INTEGER,
            slack_message_count INTEGER,
            last_seen TEXT,
            is_internal INTEGER,
            account TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO people VALUES ('alice@acme-corp.com', 'Alice Smith', 9, 4, 1, 0, '2023-11-14', 0, 'acme-corp')"
    )
    conn.commit()
    conn.close()
    return home, db_path


def test_gmail_backstory_gap_json_lists_gap_contacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """backstory-gap emits one item per gap contact, tagged with its account."""
    from fieldkit.commands.gmail import backstory_gap

    home, db_path = _backstory_env(tmp_path)
    monkeypatch.setattr(backstory_gap, "_config_path", lambda: home / "config" / "accounts.yaml")

    result = CliRunner().invoke(backstory_gap.cli, ["--db", str(db_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["items"][0]["account"] == "acme-corp"
    assert payload["items"][0]["email"] == "alice@acme-corp.com"
    assert payload["items"][0]["min_messages"] == 2


def test_gmail_backstory_gap_json_records_the_applied_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --min-messages appears both as the filter and on each item."""
    from fieldkit.commands.gmail import backstory_gap

    home, db_path = _backstory_env(tmp_path)
    monkeypatch.setattr(backstory_gap, "_config_path", lambda: home / "config" / "accounts.yaml")

    result = CliRunner().invoke(backstory_gap.cli, ["--db", str(db_path), "--min-messages", "5", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["filters"]["min_messages"] == 5
    assert payload["items"][0]["min_messages"] == 5


def test_gmail_backstory_gap_prose_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --json the markdown report still renders."""
    from fieldkit.commands.gmail import backstory_gap

    home, db_path = _backstory_env(tmp_path)
    monkeypatch.setattr(backstory_gap, "_config_path", lambda: home / "config" / "accounts.yaml")

    result = CliRunner().invoke(backstory_gap.cli, ["--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "# Backstory Gap Report" in result.output
    assert "**Total gap contacts across all accounts: 1**" in result.output


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def _driver_result(outcome: str = "ok", error: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        issue_number=1234,
        issue_title="historic regression example",
        branch="fix/historic regression",
        outcome=outcome,
        elapsed_seconds=12.5,
        spend_note="spend: $0.10",
        error=error,
    )


def test_driver_run_json_reports_the_result(tmp_path: Path) -> None:
    """driver run --json emits the run record instead of the status lines."""
    with (
        patch("fieldkit.commands.driver.cli._repo_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.run_driver", return_value=_driver_result()),
    ):
        result = CliRunner().invoke(driver_cli, ["run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["issue_number"] == 1234
    assert payload["outcome"] == "ok"
    assert payload["dry_run"] is False


def test_driver_run_json_keeps_exit_1_on_a_failed_run(tmp_path: Path) -> None:
    """A failed run still exits 1 — and still hands back the error field."""
    with (
        patch("fieldkit.commands.driver.cli._repo_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.run_driver", return_value=_driver_result("failed", "opencode exited 1")),
    ):
        result = CliRunner().invoke(driver_cli, ["run", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "failed"
    assert payload["error"] == "opencode exited 1"


def test_driver_list_json_lists_ready_issues() -> None:
    """driver list --json emits {items, count, filters}."""
    issues = [SimpleNamespace(number=7, title="implementation change example", attempt_count=2)]

    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.commands.driver.cli.list_ready_issues", return_value=issues),
    ):
        result = CliRunner().invoke(driver_cli, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["items"] == [{"number": 7, "title": "implementation change example", "attempt_count": 2}]
    assert payload["count"] == 1
    assert payload["filters"]["repo"] == "owner/repo"


def test_driver_list_json_with_no_issues_is_an_empty_document() -> None:
    """An empty queue is count 0, not the "No agent-ready issues." prose."""
    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.commands.driver.cli.list_ready_issues", return_value=[]),
    ):
        result = CliRunner().invoke(driver_cli, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["items"] == []


def _write_run_status(data_root: Path, subdir: str, filename: str, runs: list[dict[str, Any]]) -> None:
    log_dir = data_root / "logs" / subdir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / filename).write_text(json.dumps({"runs": runs}), encoding="utf-8")


def test_driver_status_json_returns_newest_first(tmp_path: Path) -> None:
    """driver status --json orders runs newest-first, matching the prose view."""
    _write_run_status(
        tmp_path,
        "driver",
        "driver-run-status.json",
        [{"outcome": "ok", "issue_number": 1, "ts": "2026-01-01T00:00:00"}, {"outcome": "failed", "issue_number": 2}],
    )

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(driver_cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["items"][0]["issue_number"] == 2
    assert payload["error"] is None


def test_driver_status_json_reports_an_unreadable_status_file(tmp_path: Path) -> None:
    """A corrupt status file exits 0 today, so the document carries the error."""
    log_dir = tmp_path / "logs" / "driver"
    log_dir.mkdir(parents=True)
    (log_dir / "driver-run-status.json").write_text("{not json", encoding="utf-8")

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(driver_cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "could not read driver run status"
    assert payload["items"] == []


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def _health_result(outcome: str = "ok") -> HealthRunResult:
    return HealthRunResult(
        outcome=outcome,
        checks_run=4,
        gate_failures=("mypy",) if outcome != "ok" else (),
        runner_errors=(),
        issues_filed=(),
        issues_deduped=(),
        elapsed_seconds=31.4,
    )


def test_health_run_json_serializes_the_result(tmp_path: Path) -> None:
    """health run --json emits the HealthRunResult fields."""
    with (
        patch("fieldkit.commands.health.cli._repo_root", return_value=tmp_path),
        patch("fieldkit.commands.health.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.commands.health.cli._GHIssueFiler"),
        patch("fieldkit.commands.health.cli.run_health", return_value=_health_result()),
    ):
        result = CliRunner().invoke(health_cli, ["run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "ok"
    assert payload["checks_run"] == 4
    assert payload["dry_run"] is False


def test_health_run_json_keeps_exit_1_on_a_degraded_run(tmp_path: Path) -> None:
    """A partial outcome still exits 1 — and still names the failed gate."""
    with (
        patch("fieldkit.commands.health.cli._repo_root", return_value=tmp_path),
        patch("fieldkit.commands.health.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.commands.health.cli._GHIssueFiler"),
        patch("fieldkit.commands.health.cli.run_health", return_value=_health_result("partial")),
    ):
        result = CliRunner().invoke(health_cli, ["run", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "partial"
    assert payload["gate_failures"] == ["mypy"]


def test_health_status_json_lists_recent_runs(tmp_path: Path) -> None:
    """health status --json emits {items, count, filters}."""
    _write_run_status(tmp_path, "health", "health-run-status.json", [{"outcome": "ok", "checks_run": 4}])

    with patch("fieldkit.commands.health.cli.get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(health_cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["items"][0]["outcome"] == "ok"
    assert payload["filters"]["limit"] == 10


def test_health_status_json_with_no_runs_is_an_empty_document(tmp_path: Path) -> None:
    """A missing status file yields count 0, not the prose notice."""
    with patch("fieldkit.commands.health.cli.get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(health_cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["items"] == []


# ---------------------------------------------------------------------------
# shadowbot / companion / web
# ---------------------------------------------------------------------------


def test_shadowbot_query_json_carries_the_unverified_caveat() -> None:
    """The not-SF-verified warning survives into the machine-readable path."""
    with (
        patch("fieldkit.commands.shadowbot.cli.auth.get_token", return_value="tok"),
        patch(
            "fieldkit.commands.shadowbot.cli.client_module.query",
            return_value=ShadowbotResponse(content="Two open opps.", thread_id="t1"),
        ),
    ):
        result = CliRunner().invoke(shadowbot_cli, ["query", "--json", "open opps?"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["content"] == "Two open opps."
    assert payload["verified"] is False
    assert "not a Salesforce system of record" in payload["notice"]


def test_shadowbot_query_json_stays_silent_on_stdout_when_auth_fails() -> None:
    """An auth failure exits 2 with nothing on stdout for a parser to misread."""
    from fieldkit.shadowbot.auth import ShadowbotAuthError

    with patch("fieldkit.commands.shadowbot.cli.auth.get_token", side_effect=ShadowbotAuthError("no token")):
        result = CliRunner().invoke(shadowbot_cli, ["query", "--json", "open opps?"])

    assert result.exit_code == 2
    assert result.stdout == ""


@pytest.mark.parametrize(("permitted", "exit_code"), [(True, 0), (False, 3)])
def test_companion_allowed_json_answers_on_both_verdicts(permitted: bool, exit_code: int) -> None:
    """Denial is an answer: the document is emitted, and exit 3 is preserved."""
    with (
        patch("fieldkit.companion.gate.is_allowed", return_value=permitted),
        patch("fieldkit.config.get_companion_tier", return_value="propose"),
        patch("fieldkit.config.get_companion_act_allowlist", return_value=[]),
    ):
        result = CliRunner().invoke(companion_cli, ["allowed", "--json", "--", "pursuit", "health"])

    assert result.exit_code == exit_code
    payload = json.loads(result.stdout)
    assert payload["allowed"] is permitted
    assert payload["tier"] == "propose"
    assert payload["command"] == ["pursuit", "health"]


def test_web_token_json_reports_the_path_and_never_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The document names the 0600 file; the secret itself stays out of stdout."""
    from fieldkit.commands.web import cli as web_cli_module

    token_path = tmp_path / "web-token"
    monkeypatch.setattr(web_cli_module, "_DEFAULT_TOKEN_PATH", token_path)

    result = CliRunner().invoke(web_cli, ["token", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["token_path"] == str(token_path)
    assert payload["mode"] == "0600"
    assert token_path.read_text(encoding="utf-8").strip() not in result.stdout
