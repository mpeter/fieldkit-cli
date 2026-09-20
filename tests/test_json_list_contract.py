"""The single source of truth for the ``--json`` list-shape contract.

Every command that returns a *collection* under ``--json`` emits the same
envelope::

    {"items": [...], "count": <len(items)>, "filters": {...}, ...}

Commands may add their own keys alongside those three (``ingest status`` adds
``db_connected``, ``pursuit archive`` adds ``archived``); what they may not do is
rename, drop, or desynchronise the three.

Why this file exists
--------------------
The contract used to be asserted independently in ``test_issue_cli_json.py``,
``test_json_flag_batch3.py`` and ``test_json_flag_rollout.py``. Three
independent transcriptions of one rule is three chances to transcribe it
differently, and that is exactly what happened: ``issue list`` shipped its
collection under ``issues`` while all fifteen sibling commands used ``items``,
and every one of the three files passed, because each only ever checked the
shape its own author had in mind.

So the rule lives here once, in :func:`_assert_list_envelope`, and every list
command is registered into ``_LIST_CASES`` below. A command that drifts fails
one assertion in one place. Adding a list command without registering it here is
the only way to reintroduce the gap — so register it.

The timestamp rule is enforced here for the same reason. ``json.dumps(...,
default=str)`` renders a ``datetime`` with a space separator
(``"2026-01-15 09:30:00+00:00"``), which is not ISO-8601 and which Go's
``time.Parse(time.RFC3339, ...)``, JS ``new Date(...)`` and ``jq``'s ``fromdate``
all reject. ``datetime.fromisoformat`` accepts it, which is precisely why a
Python-only suite never noticed — hence the explicit ``T``-separator regex in
:func:`_assert_iso8601_timestamps` rather than a bare round-trip.

Not registered: ``ingest discover``. It emits the envelope from four sites but
has no runtime ``--json`` test anywhere in the suite to model an invocation on,
and inventing one here would assert against a fixture rather than the command.
Registering it needs a working discover fixture first.
"""

import importlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.issue.gh_store import GHIssue

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: The three keys every list-shaped document must carry. Commands may add more.
_ENVELOPE_KEYS = {"items", "count", "filters"}

#: A string that opens with YYYY-MM-DD is a date or timestamp and is held to
#: ISO-8601. Anything else in the payload is free-form text.
_DATE_LEADING = re.compile(r"^\d{4}-\d{2}-\d{2}")

#: Strict ISO-8601/RFC3339: date, or date + ``T`` + time (+ optional fraction and
#: offset). The space separator ``str(datetime)`` produces does not match.
_STRICT_ISO8601 = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$",
)


def _assert_list_envelope(payload: Any, *, command: str) -> None:
    """Assert *payload* is a list-shaped document. The one definition of the rule."""
    assert isinstance(payload, dict), f"{command}: --json must emit an object, got {type(payload).__name__}"
    missing = _ENVELOPE_KEYS - set(payload)
    assert not missing, f"{command}: list document is missing {sorted(missing)} (has {sorted(payload)})"
    assert isinstance(payload["items"], list), f"{command}: 'items' must be a list"
    assert isinstance(payload["filters"], dict), f"{command}: 'filters' must be an object"
    assert payload["count"] == len(payload["items"]), (
        f"{command}: 'count' ({payload['count']}) disagrees with len(items) ({len(payload['items'])})"
    )


def _walk_strings(node: Any, path: str = "$") -> list[tuple[str, str]]:
    """Yield every (json-path, string) pair in *node*, depth-first."""
    if isinstance(node, dict):
        return [pair for k, v in node.items() for pair in _walk_strings(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [pair for i, v in enumerate(node) for pair in _walk_strings(v, f"{path}[{i}]")]
    if isinstance(node, str):
        return [(path, node)]
    return []


def _assert_iso8601_timestamps(payload: Any, *, command: str) -> None:
    """Assert every date-looking string in *payload* is strict ISO-8601."""
    for where, value in _walk_strings(payload):
        if not _DATE_LEADING.match(value):
            continue
        assert _STRICT_ISO8601.match(value), (
            f"{command}: {where} = {value!r} is not strict ISO-8601 "
            f"(a space separator instead of 'T' is the usual cause — "
            f"pass default=json_default to json.dumps, not default=str)"
        )
        # fromisoformat is the round-trip half of the check; on its own it would
        # accept the space form, which is the bug this test exists to catch.
        datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Fixtures shared by the registered commands
# ---------------------------------------------------------------------------


def _payload(result: Result, command: str) -> dict[str, Any]:
    """Assert a clean exit and return the parsed stdout document."""
    assert result.exit_code == 0, f"{command}: exit {result.exit_code}\n{result.output}"
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _write_run_status(data_root: Path, subdir: str, filename: str, runs: list[dict[str, Any]]) -> None:
    log_dir = data_root / "logs" / subdir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / filename).write_text(json.dumps({"runs": runs}), encoding="utf-8")


def _query_db() -> sqlite3.Connection:
    """An in-memory gmail.db carrying one person, message, thread and account tag."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (thread_id TEXT PRIMARY KEY, subject TEXT,
                              message_count INTEGER DEFAULT 0, updated_at TEXT);
        CREATE TABLE messages (msg_id TEXT PRIMARY KEY, thread_id TEXT, subject TEXT,
                               from_addr TEXT, to_addr TEXT, cc_addr TEXT,
                               date_str TEXT, date_epoch INTEGER, body_plain TEXT);
        CREATE TABLE people (person_id TEXT PRIMARY KEY, email TEXT, display_name TEXT,
                             message_count INTEGER DEFAULT 0);
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT);
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
            "Here is the renewal pricing you asked for.",
        ),
    )
    conn.execute(
        "INSERT INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, 1, ?)",
        ("t-001", "Renewal pricing", "2023-11-14"),
    )
    conn.execute("INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)", ("t-001", "acme-corp"))
    conn.commit()
    return conn


def _pursuit_file(tmp_path: Path, *, stage: str = "closed-won", account: str = "acme-corp") -> Path:
    pursuits = tmp_path / "accounts" / account / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    path = pursuits / "renewal.md"
    path.write_text(
        f"---\nsf_opportunity_id: 006AAA00000000AAA\nstage: {stage}\ngate-status: pending\n---\n\n# Pursuit\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The registry — one entry per list-shaped command
# ---------------------------------------------------------------------------


def _issue_list(tmp_path: Path) -> dict[str, Any]:
    # `created` is a real datetime, not a pre-formatted string: this is the one
    # registered payload that carries a live datetime through json.dumps, so it
    # is what holds the ISO-8601 rule honest.
    issue = GHIssue(
        id="historic regression",
        type="bug",
        title="something broken",
        status="open",
        severity="high",
        module="sf",
        gh_number=42,
        body="Body text.",
        source="test-agent",
        created=datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC),
    )
    store = MagicMock()
    store.list_issues.side_effect = lambda status, **kw: [issue] if status == "open" else []
    issue_cli = importlib.import_module("fieldkit.commands.issue.cli")
    with (
        patch.object(issue_cli, "_store", return_value=store),
        patch.object(issue_cli, "get_github_repo", return_value="owner/test-repo"),
    ):
        result = CliRunner().invoke(issue_cli.cli, ["list", "--json"])
    return _payload(result, "issue list")


def _issue_list_empty(tmp_path: Path) -> dict[str, Any]:
    store = MagicMock()
    store.list_issues.return_value = []
    issue_cli = importlib.import_module("fieldkit.commands.issue.cli")
    with (
        patch.object(issue_cli, "_store", return_value=store),
        patch.object(issue_cli, "get_github_repo", return_value="owner/test-repo"),
    ):
        result = CliRunner().invoke(issue_cli.cli, ["list", "--json"])
    return _payload(result, "issue list (empty)")


def _driver_list(tmp_path: Path) -> dict[str, Any]:
    driver_cli = importlib.import_module("fieldkit.commands.driver.cli")
    issues = [SimpleNamespace(number=7, title="implementation change example", attempt_count=2)]
    with (
        patch.object(driver_cli, "get_github_repo", return_value="owner/repo"),
        patch.object(driver_cli, "list_ready_issues", return_value=issues),
    ):
        result = CliRunner().invoke(driver_cli.cli, ["list", "--json"])
    return _payload(result, "driver list")


def _driver_status(tmp_path: Path) -> dict[str, Any]:
    _write_run_status(
        tmp_path,
        "driver",
        "driver-run-status.json",
        [{"outcome": "ok", "issue_number": 1, "ts": "2026-01-01T00:00:00"}],
    )
    driver_cli = importlib.import_module("fieldkit.commands.driver.cli")
    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(driver_cli.cli, ["status", "--json"])
    return _payload(result, "driver status")


def _health_status(tmp_path: Path) -> dict[str, Any]:
    _write_run_status(tmp_path, "health", "health-run-status.json", [{"outcome": "ok", "checks_run": 4}])
    health_cli = importlib.import_module("fieldkit.commands.health.cli")
    with patch.object(health_cli, "get_fieldkit_data", return_value=tmp_path):
        result = CliRunner().invoke(health_cli.cli, ["status", "--json"])
    return _payload(result, "health status")


def _watch_status(tmp_path: Path) -> dict[str, Any]:
    statuses = {
        "slack-threads": {"outcome": "partial", "last_run": "2026-07-01T06:45:01Z"},
        "draft-queue": {"outcome": "ok", "last_run": "2026-07-01T06:40:00Z"},
    }
    watch_cli = importlib.import_module("fieldkit.commands.watch.cli")
    with patch.object(watch_cli._watch_status, "load_all_statuses", return_value=statuses):
        result = CliRunner().invoke(watch_cli.status_cmd, ["--json"])
    return _payload(result, "watch status")


def _meeting_list(tmp_path: Path) -> dict[str, Any]:
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
    return _payload(result, "meeting list")


def _ingest_status(tmp_path: Path) -> dict[str, Any]:
    status = importlib.import_module("fieldkit.commands.ingest.status")
    result = CliRunner().invoke(status.cli, ["--json"])
    return _payload(result, "ingest status")


def _ingest_backfill(tmp_path: Path) -> dict[str, Any]:
    backfill = importlib.import_module("fieldkit.commands.ingest.backfill")
    meetings = tmp_path / "accounts" / "acme-corp" / "meetings"
    meetings.mkdir(parents=True)
    (meetings / "2026-07-01-qbr.md").write_text("---\ntitle: QBR\n---\n\nbody\n", encoding="utf-8")
    with patch.object(backfill, "get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(backfill.cli, ["--json"])
    return _payload(result, "ingest backfill")


def _ingest_route(tmp_path: Path) -> dict[str, Any]:
    route = importlib.import_module("fieldkit.commands.ingest.route")
    result = CliRunner().invoke(route.cli, ["--data-root", str(tmp_path), "--json"])
    return _payload(result, "ingest route")


def _pursuit_archive(tmp_path: Path) -> dict[str, Any]:
    archive = importlib.import_module("fieldkit.commands.pursuit.archive_cmd")
    _pursuit_file(tmp_path)
    with patch.object(archive, "_data_root", return_value=tmp_path):
        result = CliRunner().invoke(archive.cli, ["--account", "acme-corp", "--all-closed", "--json"])
    return _payload(result, "pursuit archive")


def _gmail_account_tags(tmp_path: Path) -> dict[str, Any]:
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
    account_tags = importlib.import_module("fieldkit.commands.gmail.account_tags")
    result = CliRunner().invoke(account_tags.cli, ["--db", str(db_path), "--json"])
    return _payload(result, "gmail account-tags")


def _gmail_backstory_gap(tmp_path: Path) -> dict[str, Any]:
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
            email TEXT PRIMARY KEY, display_name TEXT, message_count INTEGER,
            thread_count INTEGER, meeting_count INTEGER, slack_message_count INTEGER,
            last_seen TEXT, is_internal INTEGER, account TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO people VALUES ('alice@acme-corp.com', 'Alice Smith', 9, 4, 1, 0, '2023-11-14', 0, 'acme-corp')"
    )
    conn.commit()
    conn.close()
    backstory_gap = importlib.import_module("fieldkit.commands.gmail.backstory_gap")
    with patch.object(backstory_gap, "_config_path", lambda: home / "config" / "accounts.yaml"):
        result = CliRunner().invoke(backstory_gap.cli, ["--db", str(db_path), "--json"])
    return _payload(result, "gmail backstory-gap")


def _gmail_query(args: list[str], label: str) -> Callable[[Path], dict[str, Any]]:
    def _build(tmp_path: Path) -> dict[str, Any]:
        query = importlib.import_module("fieldkit.commands.gmail.query")
        with patch.object(query, "connect", return_value=_query_db()):
            result = CliRunner().invoke(query.cli, [*args, "--db", str(tmp_path / "fake.db"), "--json"])
        return _payload(result, label)

    return _build


def _sync(tmp_path: Path) -> dict[str, Any]:
    datasync = importlib.import_module("fieldkit.commands.datasync.cli")
    results = [
        datasync.StepResult(index=1, total=2, label="gmail sync", cmd=["gmail"], success=True, elapsed=1.0),
        datasync.StepResult(index=2, total=2, label="ingest", cmd=["ingest"], success=True, elapsed=2.0),
    ]
    with (
        patch.object(datasync, "run_pipeline", return_value=results),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = CliRunner().invoke(datasync.cli, ["--json"])
    # `sync` exits 0 only when every step succeeded, which is how it is fixtured here.
    return _payload(result, "sync")


#: Every command that emits a list-shaped ``--json`` document. Add new ones here.
_LIST_CASES: list[tuple[str, Callable[[Path], dict[str, Any]]]] = [
    ("issue list", _issue_list),
    ("issue list (empty)", _issue_list_empty),
    ("driver list", _driver_list),
    ("driver status", _driver_status),
    ("health status", _health_status),
    ("watch status", _watch_status),
    ("meeting list", _meeting_list),
    ("ingest status", _ingest_status),
    ("ingest backfill", _ingest_backfill),
    ("ingest route", _ingest_route),
    ("pursuit archive", _pursuit_archive),
    ("gmail account-tags", _gmail_account_tags),
    ("gmail backstory-gap", _gmail_backstory_gap),
    ("gmail query person", _gmail_query(["person", "Alice Smith"], "gmail query person")),
    ("gmail query context", _gmail_query(["context", "Alice Smith"], "gmail query context")),
    ("gmail query account", _gmail_query(["account", "acme-corp"], "gmail query account")),
    ("gmail query dig", _gmail_query(["dig", "acme-corp", "pricing"], "gmail query dig")),
    ("gmail query threads", _gmail_query(["threads", "Renewal"], "gmail query threads")),
    ("gmail query blindspots", _gmail_query(["blindspots", "acme-corp"], "gmail query blindspots")),
    ("sync", _sync),
]

_CASE_PARAMS = [pytest.param(builder, name, id=name.replace(" ", "-")) for name, builder in _LIST_CASES]


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("builder", "command"), _CASE_PARAMS)
def test_list_command_emits_the_shared_envelope(
    builder: Callable[[Path], dict[str, Any]], command: str, tmp_path: Path
) -> None:
    """Every list command emits {items, count, filters} with count == len(items)."""
    payload = builder(tmp_path)

    _assert_list_envelope(payload, command=command)


@pytest.mark.parametrize(("builder", "command"), _CASE_PARAMS)
def test_list_command_timestamps_are_strict_iso8601(
    builder: Callable[[Path], dict[str, Any]], command: str, tmp_path: Path
) -> None:
    """No list command emits `str(datetime)` — the space separator is not ISO-8601."""
    payload = builder(tmp_path)

    _assert_iso8601_timestamps(payload, command=command)


def test_the_envelope_check_rejects_a_renamed_collection_key() -> None:
    """The guard itself must fail on the exact drift that shipped (`issues`, not `items`)."""
    drifted = {"issues": [{"id": "historic regression"}], "count": 1, "filters": {}}

    with pytest.raises(AssertionError, match=r"missing \['items'\]"):
        _assert_list_envelope(drifted, command="regression probe")


def test_the_envelope_check_rejects_a_count_that_disagrees_with_items() -> None:
    """`count` is a claim about `items`; a stale one is worse than no count at all."""
    drifted = {"items": [{"id": "historic regression"}], "count": 7, "filters": {}}

    with pytest.raises(AssertionError, match="disagrees with len"):
        _assert_list_envelope(drifted, command="regression probe")


def test_the_timestamp_check_rejects_the_str_datetime_rendering() -> None:
    """`str(datetime)` uses a space separator; fromisoformat alone would accept it."""
    rendered = {"items": [{"created": str(datetime(2026, 1, 15, 9, 30, tzinfo=UTC))}], "count": 1, "filters": {}}

    with pytest.raises(AssertionError, match="not strict ISO-8601"):
        _assert_iso8601_timestamps(rendered, command="regression probe")


def test_the_timestamp_check_accepts_isoformat_and_bare_dates() -> None:
    """The guard must not fire on the correct rendering, or it would block the fix."""
    correct = {
        "items": [{"created": datetime(2026, 1, 15, 9, 30, tzinfo=UTC).isoformat(), "day": "2026-01-15"}],
        "count": 1,
        "filters": {},
    }

    _assert_iso8601_timestamps(correct, command="regression probe")
