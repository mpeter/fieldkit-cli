"""Two-account filter tests for implementation change, implementation change, implementation change, implementation change, implementation change.

Each test creates fixtures for two accounts, runs the command filtered to one,
and asserts the second account's data is absent from the output/effect.
All tests are @pytest.mark.unit with tmp_path DB fixtures; no live I/O.
"""

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_GMAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    subject       TEXT,
    snippet       TEXT,
    message_count INTEGER DEFAULT 0,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    subject     TEXT,
    date_str    TEXT,
    date_epoch  INTEGER,
    labels      TEXT DEFAULT '[]',
    body_plain  TEXT DEFAULT '',
    body_html   TEXT DEFAULT '',
    size_bytes  INTEGER DEFAULT 0,
    snippet     TEXT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS labels (
    label_id    TEXT PRIMARY KEY,
    label_name  TEXT
);
CREATE TABLE IF NOT EXISTS thread_accounts (
    thread_id   TEXT,
    account     TEXT,
    PRIMARY KEY (thread_id, account)
);
"""

_PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id   TEXT PRIMARY KEY,
    description   TEXT NOT NULL,
    version       TEXT NOT NULL,
    source_format TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    pipeline_id   TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    file_hash     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at  TEXT,
    meeting_title TEXT,
    meeting_date  TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id   TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    pipeline_id   TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    content_path  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_gmail_db(path: Path) -> None:
    """Create a gmail.db with Gemini notes for two accounts: acme and globalpay."""
    import time

    conn = sqlite3.connect(str(path))
    conn.executescript(_GMAIL_SCHEMA)
    now = int(time.time())
    msgs = [
        # acme-account messages (to_addr contains acme.example.com domain)
        (
            "msg_acme_1",
            "msg_acme_1",
            "Gemini <gemini-notes@google.com>",
            "alice@acme.example.com, bob@acme.example.com",
            "",
            'Notes: "Acme Strategy Meeting"',
            now - 86400,
            "https://docs.google.com/document/d/ACME_DOC_001/edit",
        ),
        # globalpay-account messages (to_addr contains globalpay.example.com domain)
        (
            "msg_global_1",
            "msg_global_1",
            "Gemini <gemini-notes@google.com>",
            "carol@globalpay.example.com, dave@globalpay.example.com",
            "",
            'Notes: "GlobalPay Review"',
            now - 86400 * 2,
            "https://docs.google.com/document/d/GLOBAL_DOC_001/edit",
        ),
    ]
    for msg_id, thread_id, from_addr, to_addr, cc_addr, subject, epoch, doc_url in msgs:
        doc_url.split("/d/")[1].split("/")[0]
        conn.execute(
            "INSERT OR IGNORE INTO threads (thread_id, subject) VALUES (?, ?)",
            (thread_id, subject),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (message_id, thread_id, from_addr, to_addr, cc_addr, subject, body_html, body_plain, date_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                thread_id,
                from_addr,
                to_addr,
                cc_addr,
                subject,
                f'<p>View notes: <a href="{doc_url}">Open</a></p>',
                f"View notes: {doc_url}",
                epoch,
            ),
        )
    conn.commit()
    conn.close()


def _make_gmail_db_with_labels(path: Path) -> None:
    """Create a gmail.db with ref/* labels for two accounts."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_GMAIL_SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO labels (label_id, label_name) VALUES (?, ?)",
        [
            ("L_ACME", "ref/acme"),
            ("L_GLOBAL", "ref/globalpay"),
        ],
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO messages
            (message_id, thread_id, from_addr, labels, body_plain, date_epoch)
        VALUES (?, ?, ?, ?, ?, 1700000000)
        """,
        [
            ("msg_acme_1", "thread_acme_1", "sender@acme.example.com", '["L_ACME"]', "acme email"),
            ("msg_acme_2", "thread_acme_2", "sender2@acme.example.com", '["L_ACME"]', "acme email 2"),
            ("msg_global_1", "thread_global_1", "sender@globalpay.example.com", '["L_GLOBAL"]', "global email"),
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO threads (thread_id, subject) VALUES (?, ?)",
        [
            ("thread_acme_1", "Acme Thread 1"),
            ("thread_acme_2", "Acme Thread 2"),
            ("thread_global_1", "GlobalPay Thread 1"),
        ],
    )
    conn.commit()
    conn.close()


def _make_pipeline_db(path: Path) -> None:
    """Create a minimal pipeline.db."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_PIPELINE_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO pipelines (pipeline_id, description, version, source_format, status) "
        "VALUES ('transcript-ingest', 'Test pipeline', '0.1.0', 'gdoc', 'active')"
    )
    conn.commit()
    conn.close()


def _make_pursuit_md(path: Path, stage: str = "propose", acv: float = 100000.0) -> None:
    """Write a minimal valid pursuit markdown file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: {stage}\nsf_acv: {acv}\n---\n\n# Pursuit\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# implementation change: ingest discover --account filter
# ---------------------------------------------------------------------------


def test_enh424_discover_unknown_account_exits_3(tmp_path: Path) -> None:
    """ingest discover --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.ingest.discover import cli as discover_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(discover_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output
    # Known slugs listed
    assert "acme" in result.output
    assert "globalpay" in result.output


def test_enh424_discover_dryrun_account_filter_excludes_other_account(tmp_path: Path) -> None:
    """ingest discover --dry-run --account acme only reports acme-routed sources."""
    gmail_db = tmp_path / "gmail.db"
    _make_gmail_db(gmail_db)

    accounts_cfg = {
        "accounts": {
            "acme": {"domains": ["acme.example.com"]},
            "globalpay": {"domains": ["globalpay.example.com"]},
        }
    }

    with (
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]),
        patch("fieldkit.ingest.router.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.ingest.router.get_internal_domains", return_value=[]),
    ):
        from fieldkit.commands.ingest.discover import cli as discover_cli

        result = CliRunner().invoke(
            discover_cli,
            ["--pipeline", "transcript-ingest", "--dry-run", "--account", "acme"],
        )

    assert result.exit_code == 0, result.output
    # acme doc should appear; globalpay doc should not
    assert "ACME_DOC_001" in result.output
    assert "GLOBAL_DOC_001" not in result.output


def test_enh424_discover_unfiltered_shows_all(tmp_path: Path) -> None:
    """ingest discover --dry-run without --account shows both accounts' sources."""
    gmail_db = tmp_path / "gmail.db"
    _make_gmail_db(gmail_db)

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=gmail_db):
        from fieldkit.commands.ingest.discover import cli as discover_cli

        result = CliRunner().invoke(
            discover_cli,
            ["--pipeline", "transcript-ingest", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    # Both docs should appear
    assert "ACME_DOC_001" in result.output
    assert "GLOBAL_DOC_001" in result.output


# ---------------------------------------------------------------------------
# implementation change: ingest status --account filter (slug validation)
# ---------------------------------------------------------------------------


def test_enh424_status_unknown_account_exits_3() -> None:
    """ingest status --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.ingest.status import cli as status_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(status_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


def test_enh424_status_valid_account_exits_0() -> None:
    """ingest status --account with valid slug → exit 0 (pipeline output unchanged)."""
    from fieldkit.commands.ingest.status import cli as status_cli

    with (
        patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]),
        patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("no db")),
    ):
        result = CliRunner().invoke(status_cli, ["--account", "acme"])

    assert result.exit_code == 0
    assert "transcript-ingest" in result.output


# ---------------------------------------------------------------------------
# implementation change: pipeline quota --account filter
# ---------------------------------------------------------------------------


def test_enh429_quota_unknown_account_exits_3() -> None:
    """pipeline quota --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.pipeline.cli import cmd_quota

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(cmd_quota, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


def test_enh429_quota_account_filter_excludes_other_account_pursuit(tmp_path: Path) -> None:
    """pipeline quota --account acme excludes globalpay pursuit from totals."""
    # Create pursuits for two accounts
    acme_dir = tmp_path / "accounts" / "acme" / "pursuits"
    global_dir = tmp_path / "accounts" / "globalpay" / "pursuits"
    _make_pursuit_md(acme_dir / "acme-deal.md", stage="propose", acv=200000.0)
    _make_pursuit_md(global_dir / "global-deal.md", stage="propose", acv=999999.0)

    quota_cfg = {"target": 1_000_000, "period": "FY2026"}

    with (
        patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]),
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=quota_cfg),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=tmp_path),
    ):
        from fieldkit.commands.pipeline.cli import cmd_quota

        result = CliRunner().invoke(cmd_quota, ["--account", "acme"])

    assert result.exit_code == 0, result.output
    # The global pursuit (ACV 999999) must NOT contribute to the weighted total
    # when filtered to acme only. The acme pursuit contributes 200000 * weight.
    assert "Target is global" in result.output
    assert "acme" in result.output
    # If globalpay were included, weighted would be much larger — verify it's bounded
    combined = result.output
    # The gap should reflect only acme's ACV, not globalpay's
    assert "999" not in combined.replace(",", ""), (
        "globalpay ACV (999999) should not appear in acme-filtered quota output"
    )


def test_enh429_quota_unfiltered_includes_all_accounts(tmp_path: Path) -> None:
    """pipeline quota without --account includes all accounts' pursuits."""
    acme_dir = tmp_path / "accounts" / "acme" / "pursuits"
    global_dir = tmp_path / "accounts" / "globalpay" / "pursuits"
    _make_pursuit_md(acme_dir / "acme-deal.md", stage="propose", acv=200000.0)
    _make_pursuit_md(global_dir / "global-deal.md", stage="propose", acv=300000.0)

    quota_cfg = {"target": 1_000_000, "period": "FY2026"}

    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=quota_cfg),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=tmp_path),
    ):
        from fieldkit.commands.pipeline.cli import cmd_quota

        result = CliRunner().invoke(cmd_quota, [])

    assert result.exit_code == 0, result.output
    # Without filter, both accounts contribute — weighted should be non-zero
    assert "Gap" in result.output


# ---------------------------------------------------------------------------
# implementation change: brief generate --account filter (slug validation)
# ---------------------------------------------------------------------------


def test_enh467_morning_brief_unknown_account_exits_3() -> None:
    """brief generate --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.brief.cli import cli as brief_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(brief_cli, ["generate", "--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


def test_enh467_morning_brief_account_filter_limits_pursuit_rows(tmp_path: Path) -> None:
    """collect_all_pursuit_data with account_filter only returns that account's pursuits."""
    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    # Create pursuits for two accounts
    acme_dir = tmp_path / "accounts" / "acme" / "pursuits"
    global_dir = tmp_path / "accounts" / "globalpay" / "pursuits"
    _make_pursuit_md(acme_dir / "acme-deal.md", stage="propose", acv=200000.0)
    _make_pursuit_md(global_dir / "global-deal.md", stage="propose", acv=300000.0)

    with patch("fieldkit.commands.pipeline.collect.get_gmail_db_path") as mock_path:
        mock_path.return_value = tmp_path / "nonexistent_gmail.db"
        rows, _signals, _blindspots = collect_all_pursuit_data(tmp_path, account_filter="acme")

    acme_deals = [r for r in rows if r.account == "acme"]
    global_deals = [r for r in rows if r.account == "globalpay"]
    assert len(acme_deals) == 1, f"Expected 1 acme deal, got {len(acme_deals)}"
    assert len(global_deals) == 0, f"Expected 0 globalpay deals when filtered to acme, got {len(global_deals)}"


def test_pipeline_account_filter_limits_blindspot_data_to_selected_account(tmp_path: Path) -> None:
    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    accounts_config = {
        "accounts": {
            "acme": {"blindspot_threshold": 2},
            "globalpay": {"blindspot_threshold": 1},
        }
    }
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=tmp_path / "missing.db"),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value=accounts_config),
    ):
        result = collect_all_pursuit_data(tmp_path, account_filter="acme")

    assert result[2] == [{"account": "acme", "active_pursuits": 0, "threshold": 2, "status": "blindspot"}]


def test_enh467_morning_brief_unfiltered_collects_all_accounts(tmp_path: Path) -> None:
    """collect_all_pursuit_data without filter returns pursuits from all accounts."""
    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    acme_dir = tmp_path / "accounts" / "acme" / "pursuits"
    global_dir = tmp_path / "accounts" / "globalpay" / "pursuits"
    _make_pursuit_md(acme_dir / "acme-deal.md", stage="propose", acv=200000.0)
    _make_pursuit_md(global_dir / "global-deal.md", stage="propose", acv=300000.0)

    with patch("fieldkit.commands.pipeline.collect.get_gmail_db_path") as mock_path:
        mock_path.return_value = tmp_path / "nonexistent_gmail.db"
        rows, _signals, _blindspots = collect_all_pursuit_data(tmp_path)

    accounts = {r.account for r in rows}
    assert "acme" in accounts
    assert "globalpay" in accounts


# ---------------------------------------------------------------------------
# implementation change: watch draft-queue --account filter
# ---------------------------------------------------------------------------


def test_enh475_draft_queue_unknown_account_exits_3() -> None:
    """watch draft-queue --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.watch.draft_queue import cli as dq_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(dq_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


def test_enh475_draft_queue_account_filter_excludes_other_account_draft(tmp_path: Path) -> None:
    """draft-queue --account acme excludes drafts addressed to globalpay recipients."""
    from fieldkit.commands.watch import draft_queue as dq

    # Two draft messages: one to acme, one to globalpay
    draft_acme = {"id": "draft_acme_1", "subject": "Acme Proposal", "to": "alice@acme.example.com", "age": "2d"}
    draft_global = {
        "id": "draft_global_1",
        "subject": "GlobalPay Proposal",
        "to": "carol@globalpay.example.com",
        "age": "2d",
    }

    accounts_cfg = {
        "accounts": {
            "acme": {"domains": ["acme.example.com"]},
            "globalpay": {"domains": ["globalpay.example.com"]},
        }
    }

    written_alerts: list[Any] = []

    def _fake_write_alerts(drafts: list[Any], *, dry_run: bool) -> None:
        written_alerts.extend(drafts)

    with (
        patch(
            "fieldkit.watch.draft_queue.get_user_email_from_env",
            return_value="user@internal.example.com",  # pii-guard: ignore
        ),
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as mock_mcp,
        patch("fieldkit.watch.draft_queue.parse_drafts", return_value=[draft_acme, draft_global]),
        patch("fieldkit.watch.draft_queue.write_alerts", side_effect=_fake_write_alerts),
        patch("fieldkit.watch.draft_queue.write_run_status"),
        patch("fieldkit.ingest.router.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.ingest.router.get_internal_domains", return_value=[]),
    ):
        mock_session = MagicMock()
        mock_mcp.return_value = mock_session
        mock_session.call_tool.return_value = []

        rc = dq._run_draft_queue(dry_run=False, account="acme")

    assert rc == 0
    assert len(written_alerts) == 1, f"Expected 1 draft (acme only), got {len(written_alerts)}: {written_alerts}"
    assert written_alerts[0]["to"] == "alice@acme.example.com"  # pii-guard: ignore


def test_enh475_draft_queue_unfiltered_includes_all_drafts(tmp_path: Path) -> None:
    """draft-queue without --account includes drafts from all accounts."""
    from fieldkit.commands.watch import draft_queue as dq

    draft_acme = {"id": "draft_acme_1", "subject": "Acme Proposal", "to": "alice@acme.example.com", "age": "2d"}
    draft_global = {
        "id": "draft_global_1",
        "subject": "GlobalPay Proposal",
        "to": "carol@globalpay.example.com",
        "age": "2d",
    }

    written_alerts: list[Any] = []

    def _fake_write_alerts(drafts: list[Any], *, dry_run: bool) -> None:
        written_alerts.extend(drafts)

    with (
        patch(
            "fieldkit.watch.draft_queue.get_user_email_from_env",
            return_value="user@internal.example.com",  # pii-guard: ignore
        ),
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as mock_mcp,
        patch("fieldkit.watch.draft_queue.parse_drafts", return_value=[draft_acme, draft_global]),
        patch("fieldkit.watch.draft_queue.write_alerts", side_effect=_fake_write_alerts),
        patch("fieldkit.watch.draft_queue.write_run_status"),
    ):
        mock_session = MagicMock()
        mock_mcp.return_value = mock_session
        mock_session.call_tool.return_value = []

        rc = dq._run_draft_queue(dry_run=False, account=None)

    assert rc == 0
    assert len(written_alerts) == 2, f"Expected 2 drafts (all accounts), got {len(written_alerts)}"


# ---------------------------------------------------------------------------
# implementation change: gmail account-tags --account filter
# ---------------------------------------------------------------------------


def test_enh414_account_tags_unknown_account_exits_3() -> None:
    """gmail account-tags --account with unknown slug → exit 3, error message."""
    from fieldkit.commands.gmail.account_tags import cli as tags_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(tags_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


def test_enh414_account_tags_filter_processes_only_target_label(tmp_path: Path) -> None:
    """account-tags --account acme only processes ref/acme labels, not ref/globalpay."""
    gmail_db = tmp_path / "gmail.db"
    _make_gmail_db_with_labels(gmail_db)

    from fieldkit.commands.gmail.account_tags import build_account_tags

    build_account_tags(str(gmail_db), account_filter="acme")

    conn = sqlite3.connect(str(gmail_db))
    rows = conn.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id").fetchall()
    conn.close()

    accounts_in_db = {r[1] for r in rows}
    thread_ids_in_db = {r[0] for r in rows}

    # Only acme threads should appear
    assert accounts_in_db == {"acme"}, f"Expected only 'acme', got {accounts_in_db}"
    assert "thread_global_1" not in thread_ids_in_db, "globalpay thread must not be tagged when filtering to acme"
    assert "thread_acme_1" in thread_ids_in_db
    assert "thread_acme_2" in thread_ids_in_db


def test_enh414_account_tags_unfiltered_processes_all_labels(tmp_path: Path) -> None:
    """account-tags without --account processes all ref/* labels."""
    gmail_db = tmp_path / "gmail.db"
    _make_gmail_db_with_labels(gmail_db)

    from fieldkit.commands.gmail.account_tags import build_account_tags

    build_account_tags(str(gmail_db), account_filter=None)

    conn = sqlite3.connect(str(gmail_db))
    rows = conn.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id").fetchall()
    conn.close()

    accounts_in_db = {r[1] for r in rows}
    assert "acme" in accounts_in_db
    assert "globalpay" in accounts_in_db


# ---------------------------------------------------------------------------
# implementation change: datasync propagates --account to gmail account-tags
# ---------------------------------------------------------------------------


def test_enh414_datasync_propagates_account_to_gmail_tags() -> None:
    """datasync _build_steps includes --account in gmail account-tags when cfg.account is set."""
    import fieldkit.commands.datasync.cli as datasync_cli

    class FakeCfg:
        account = "acme"
        quick = False
        sf = False

    steps = datasync_cli._build_steps(FakeCfg())  # type: ignore[attr-defined]

    # Find the account-tags step
    tags_step = next((cmd for label, cmd in steps if label == "account-tags"), None)
    assert tags_step is not None, "account-tags step missing from datasync steps"
    assert "--account" in tags_step, f"--account not in account-tags command: {tags_step}"
    assert "acme" in tags_step, f"account slug 'acme' not in account-tags command: {tags_step}"


def test_enh414_datasync_no_account_omits_flag_from_gmail_tags() -> None:
    """datasync _build_steps omits --account from gmail account-tags when cfg.account is None."""
    import fieldkit.commands.datasync.cli as datasync_cli

    class FakeCfg:
        account = None
        quick = False
        sf = False

    steps = datasync_cli._build_steps(FakeCfg())  # type: ignore[attr-defined]

    tags_step = next((cmd for label, cmd in steps if label == "account-tags"), None)
    assert tags_step is not None, "account-tags step missing from datasync steps"
    # When account is None, --account must NOT appear
    assert "--account" not in tags_step, f"--account should not appear in unfiltered account-tags: {tags_step}"


def test_enh475_draft_queue_rfc5322_displayname_to_header_matches(tmp_path: Path) -> None:
    """RFC 5322 display-name To: headers (e.g. 'Alice <alice@acme.example.com>') must match.

    Regression for adversary finding: _normalize_domains was stripping @-local-part
    but leaving a trailing '>' from angle-bracket notation, producing 'acme.example.com>'
    which never matches the configured domain 'acme.example.com'.
    """
    from fieldkit.commands.watch import draft_queue as dq

    # RFC 5322 display-name format — common from Gmail API
    draft_displayname = {
        "id": "draft_rfc5322_1",
        "subject": "Acme Proposal",
        "to": "Alice Smith <alice@acme.example.com>",  # pii-guard: ignore
        "age": "2d",
    }
    draft_other = {
        "id": "draft_other_1",
        "subject": "GlobalPay Proposal",
        "to": "carol@globalpay.example.com",  # pii-guard: ignore
        "age": "2d",
    }

    accounts_cfg = {
        "accounts": {
            "acme": {"domains": ["acme.example.com"]},
            "globalpay": {"domains": ["globalpay.example.com"]},
        }
    }

    written_alerts: list[Any] = []

    def _fake_write_alerts(drafts: list[Any], *, dry_run: bool) -> None:
        written_alerts.extend(drafts)

    with (
        patch(
            "fieldkit.watch.draft_queue.get_user_email_from_env",
            return_value="user@internal.example.com",  # pii-guard: ignore
        ),
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as mock_mcp,
        patch("fieldkit.watch.draft_queue.parse_drafts", return_value=[draft_displayname, draft_other]),
        patch("fieldkit.watch.draft_queue.write_alerts", side_effect=_fake_write_alerts),
        patch("fieldkit.watch.draft_queue.write_run_status"),
        patch("fieldkit.ingest.router.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.ingest.router.get_internal_domains", return_value=[]),
    ):
        mock_session = MagicMock()
        mock_mcp.return_value = mock_session
        mock_session.call_tool.return_value = []

        rc = dq._run_draft_queue(dry_run=False, account="acme")

    assert rc == 0
    assert len(written_alerts) == 1, (
        f"RFC 5322 display-name To: header must match 'acme' account. "
        f"Got {len(written_alerts)} drafts (expected 1): {written_alerts}"
    )
    assert "alice@acme.example.com" in written_alerts[0]["to"]  # pii-guard: ignore


# ---------------------------------------------------------------------------
# BUG: datasync passed --account to `gmail sync`, which does not accept it
# ---------------------------------------------------------------------------


def _leaf_for_argv(argv: list[str]):  # type: ignore[no-untyped-def]
    """Resolve a datasync step argv to the Click command it will actually invoke."""
    from fieldkit.cli_registry import walk_cli

    # argv is [python, "-m", "fieldkit", <group>, <sub>, ...]; drop the launcher.
    words = argv[3:]
    path = [w for w in words if not w.startswith("-")]
    nodes = {n.full_name: n for n in walk_cli()}
    # Longest matching command path wins — steps pass positional values too.
    for end in range(len(path), 0, -1):
        node = nodes.get(" ".join(path[:end]))
        if node is not None:
            return node
    return None


def _flags_in(argv: list[str]) -> list[str]:
    return [w for w in argv if w.startswith("--")]


def test_every_flag_datasync_passes_is_accepted_by_its_subcommand() -> None:
    """No step may be built with a flag its target command rejects.

    `fieldkit sync --account <slug>` built `gmail sync --account <slug>`, which
    `gmail sync` has never accepted. Click exited "No such option: --account",
    so the first step of every account-scoped sync failed and took the run to
    exit 1. The pre-existing plumbing test only checked the `account-tags` step,
    so the broken sibling went unnoticed — this checks all of them, which is the
    property that actually prevents recurrence.
    """
    import click

    import fieldkit.commands.datasync.cli as datasync_cli

    cfg = datasync_cli.RunConfig(quick=False, sf=True, account="acme")
    offenders: list[str] = []

    for label, argv in datasync_cli._build_steps(cfg):
        if not argv:
            continue  # marker steps carry no command
        node = _leaf_for_argv(argv)
        if node is None:
            continue
        accepted = {opt for p in node.command.params if isinstance(p, click.Option) for opt in p.opts}
        for flag in _flags_in(argv):
            if flag not in accepted:
                offenders.append(f"{label}: `fieldkit {node.full_name}` does not accept {flag}")

    assert not offenders, "datasync builds commands with unsupported flags:\n  " + "\n  ".join(offenders)


def test_gmail_sync_step_is_not_account_scoped() -> None:
    """The cache-filling step stays unscoped even when --account is given.

    Scoping it would leave gmail.db holding only one account's mail, and every
    later unscoped query would read that gap as an absence of mail rather than
    an absence of sync.
    """
    import fieldkit.commands.datasync.cli as datasync_cli

    steps = dict(datasync_cli._build_steps(datasync_cli.RunConfig(account="acme")))
    assert "--account" not in steps["gmail sync"]


def test_analysis_steps_are_account_scoped() -> None:
    """The steps that can be scoped still are — the fix must not disable filtering."""
    import fieldkit.commands.datasync.cli as datasync_cli

    steps = dict(datasync_cli._build_steps(datasync_cli.RunConfig(account="acme")))
    for label in ("account-tags", "enrich-pursuits", "backstory-health", "pursuit-stalls", "slack-threads"):
        assert "--account" in steps[label], f"{label} lost its account scoping"
        assert "acme" in steps[label]


# ---------------------------------------------------------------------------
# D3.2 gap closure: pursuit repair-dates, meeting list, ingest backfill,
# gmail query threads. Same shape as the tests above — two accounts, filter to
# one, assert the other is absent.
# ---------------------------------------------------------------------------


def _make_pursuit_tree(root: Path) -> None:
    """accounts/{acme,globalpay}/pursuits/ with one pursuit each."""
    for slug, name in (("acme", "acme-deal"), ("globalpay", "global-deal")):
        _make_pursuit_md(root / "accounts" / slug / "pursuits" / f"{name}.md")


# ── pursuit repair-dates ────────────────────────────────────────────────────


def test_repair_dates_account_filter_excludes_other_account(tmp_path: Path) -> None:
    from fieldkit.watch import repair

    _make_pursuit_tree(tmp_path)

    with patch.object(repair, "_accounts_dir", return_value=tmp_path / "accounts"):
        acme_only = repair._all_pursuit_files("acme")
        everything = repair._all_pursuit_files()

    assert [p.name for p in acme_only] == ["acme-deal.md"]
    assert {p.name for p in everything} == {"acme-deal.md", "global-deal.md"}


def test_repair_dates_unknown_account_exits_3() -> None:
    from fieldkit.commands.pursuit.repair_dates_cmd import cli as repair_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(repair_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()
    assert "no-such-account" in result.output


# ── meeting list ────────────────────────────────────────────────────────────


def _make_workbook_pursuit(path: Path, doc_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ngdoc_workbook: {doc_id}\n---\n\n# Pursuit\n", encoding="utf-8")


def test_meeting_list_account_filter_excludes_other_account(tmp_path: Path) -> None:
    from fieldkit.meeting.docs_domain import list_meetings

    _make_workbook_pursuit(tmp_path / "accounts" / "acme" / "pursuits" / "acme-deal.md", "DOC_ACME")
    _make_workbook_pursuit(tmp_path / "accounts" / "globalpay" / "pursuits" / "global-deal.md", "DOC_GLOBAL")

    scoped = list_meetings(tmp_path, "acme")
    unscoped = list_meetings(tmp_path)

    assert [e.relative_path.parts[1] for e in scoped] == ["acme"]
    assert {e.relative_path.parts[1] for e in unscoped} == {"acme", "globalpay"}


def test_meeting_list_unknown_account_exits_3() -> None:
    from fieldkit.commands.meeting.list_cmd import cli as list_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(list_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()


# ── ingest backfill ─────────────────────────────────────────────────────────


def _make_unstamped_meeting(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: Meeting\n---\n\n# Notes\n", encoding="utf-8")


def test_ingest_backfill_account_filter_excludes_other_account(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.backfill import scan_vault

    accounts = tmp_path / "accounts"
    _make_unstamped_meeting(accounts / "acme" / "meetings" / "acme-sync.md")
    _make_unstamped_meeting(accounts / "globalpay" / "meetings" / "global-sync.md")

    scoped = scan_vault(accounts, "acme")
    unscoped = scan_vault(accounts)

    assert [c.account for c in scoped] == ["acme"]
    assert {c.account for c in unscoped} == {"acme", "globalpay"}


def test_ingest_backfill_unknown_account_exits_3() -> None:
    from fieldkit.commands.ingest.backfill import cli as backfill_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(backfill_cli, ["--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()


# ── gmail query threads ─────────────────────────────────────────────────────


def _make_gmail_db_with_thread_accounts(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_GMAIL_SCHEMA)
    # query_domain.connect() migrates indexes on open, which touches `people`;
    # the shared _GMAIL_SCHEMA above predates that and omits it.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS people ( email TEXT PRIMARY KEY, display_name TEXT, last_seen_epoch INTEGER);"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO threads (thread_id, subject, message_count) VALUES (?, ?, ?)",
        [
            ("t_acme", "Quarterly review with Acme", 1),
            ("t_global", "Quarterly review with GlobalPay", 1),
            ("t_untagged", "Quarterly review unfiled", 1),
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO messages (message_id, thread_id, date_str, date_epoch) VALUES (?, ?, ?, ?)",
        [
            ("m_acme", "t_acme", "2026-01-01", 1767225600),
            ("m_global", "t_global", "2026-01-02", 1767312000),
            ("m_untagged", "t_untagged", "2026-01-03", 1767398400),
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO thread_accounts (thread_id, account) VALUES (?, ?)",
        [("t_acme", "acme"), ("t_global", "globalpay")],
    )
    conn.commit()
    conn.close()


def test_gmail_query_threads_account_filter_excludes_other_account(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.query import cmd_threads_click

    db = tmp_path / "gmail.db"
    _make_gmail_db_with_thread_accounts(db)

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(cmd_threads_click, ["Quarterly", "--account", "acme", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "Acme" in result.output
    assert "GlobalPay" not in result.output


def test_gmail_query_threads_unfiltered_includes_untagged_threads(tmp_path: Path) -> None:
    """No --account must not silently join thread_accounts and drop untagged threads.

    The join is the filter; adding it unconditionally would make an unscoped
    search quietly narrower than it used to be.
    """
    from fieldkit.commands.gmail.query import cmd_threads_click

    db = tmp_path / "gmail.db"
    _make_gmail_db_with_thread_accounts(db)

    result = CliRunner().invoke(cmd_threads_click, ["Quarterly", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "Acme" in result.output
    assert "GlobalPay" in result.output
    assert "unfiled" in result.output


def test_gmail_query_threads_unknown_account_exits_3(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.query import cmd_threads_click

    db = tmp_path / "gmail.db"
    _make_gmail_db_with_thread_accounts(db)

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(cmd_threads_click, ["Quarterly", "--account", "nope", "--db", str(db)])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()


# ---------------------------------------------------------------------------
# ingest reprocess --account: recovered from content_path, not a schema change
# ---------------------------------------------------------------------------


def _make_reprocess_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_PIPELINE_SCHEMA)
    conn.execute("ALTER TABLE artifacts ADD COLUMN pipeline_version TEXT")
    rows = [
        ("a_acme", "s1", "/data/accounts/acme/meetings/one.md"),
        ("a_acme2", "s2", "/data/accounts/acme/meetings/two.md"),
        ("a_global", "s3", "/data/accounts/globalpay/meetings/three.md"),
        ("a_unknown", "s4", "/data/accounts/unknown/meetings/four.md"),
        ("a_nopath", "s5", None),
    ]
    for aid, sid, cpath in rows:
        conn.execute(
            "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, "
            "content_path, pipeline_version) VALUES (?, ?, 'transcript-ingest', 'note', ?, '0.1.0')",
            (aid, sid, cpath),
        )
    conn.commit()
    conn.close()


def test_reprocess_account_filter_excludes_other_account(tmp_path: Path) -> None:
    from fieldkit.ingest.db import get_artifacts_for_reprocess

    db = tmp_path / "pipeline.db"
    _make_reprocess_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    scoped = get_artifacts_for_reprocess(conn, "transcript-ingest", account="acme")
    unscoped = get_artifacts_for_reprocess(conn, "transcript-ingest")
    conn.close()

    assert {a.artifact_id for a in scoped} == {"a_acme", "a_acme2"}
    assert {a.artifact_id for a in unscoped} == {"a_acme", "a_acme2", "a_global", "a_unknown", "a_nopath"}


def test_reprocess_account_filter_is_segment_bounded(tmp_path: Path) -> None:
    """`acme` must not match `acme-corp` — the slug is a whole path segment."""
    from fieldkit.ingest.db import get_artifacts_for_reprocess

    db = tmp_path / "pipeline.db"
    _make_reprocess_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, "
        "content_path, pipeline_version) VALUES ('a_corp', 's6', 'transcript-ingest', 'note', "
        "'/data/accounts/acme-corp/meetings/five.md', '0.1.0')"
    )
    conn.commit()

    scoped = get_artifacts_for_reprocess(conn, "transcript-ingest", account="acme")
    conn.close()

    assert "a_corp" not in {a.artifact_id for a in scoped}


def test_reprocess_account_filter_excludes_artifacts_with_no_path(tmp_path: Path) -> None:
    """An artifact never written to a vault path has no account to match."""
    from fieldkit.ingest.db import get_artifacts_for_reprocess

    db = tmp_path / "pipeline.db"
    _make_reprocess_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    scoped = get_artifacts_for_reprocess(conn, "transcript-ingest", account="acme")
    conn.close()
    assert "a_nopath" not in {a.artifact_id for a in scoped}


def test_reprocess_unknown_account_exits_3() -> None:
    from fieldkit.commands.ingest.reprocess import cli as reprocess_cli

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(reprocess_cli, ["--pipeline", "transcript-ingest", "--account", "no-such-account"])

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()


# ---------------------------------------------------------------------------
# ingest route --file/--force-account: operator override for one leftover
# ---------------------------------------------------------------------------


def _make_unknown_meeting(root: Path, name: str, extra_fm: str = "") -> Path:
    d = root / "accounts" / "unknown" / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(f"---\ntitle: Some Meeting\n{extra_fm}---\n\n# Notes\n", encoding="utf-8")
    return f


def test_force_account_moves_the_named_file(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")
    _make_unknown_meeting(tmp_path, "bystander.md")

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "target.md", "--force-account", "acme"],
        )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "accounts" / "acme" / "meetings" / "target.md").is_file()
    assert not (tmp_path / "accounts" / "unknown" / "meetings" / "target.md").exists()
    # The other leftover is untouched — a force is not a sweep.
    assert (tmp_path / "accounts" / "unknown" / "meetings" / "bystander.md").is_file()


def test_force_account_overrides_the_reviewed_unmatched_guard(tmp_path: Path) -> None:
    """The guard marks exactly the files a manual force exists to resolve.

    Honouring it would make the command a no-op on its only real input.
    """
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "stuck.md", extra_fm="reviewed-unmatched: true\n")

    with patch("fieldkit.config.get_account_names", return_value=["acme"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "stuck.md", "--force-account", "acme"],
        )

    assert result.exit_code == 0, result.output
    moved = tmp_path / "accounts" / "acme" / "meetings" / "stuck.md"
    assert moved.is_file()
    text = moved.read_text()
    assert "account: acme" in text
    assert "reviewed-unmatched" not in text, "stale guard key left on a resolved file"


def test_force_account_clears_ambiguous_match(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "amb.md", extra_fm="ambiguous-match: [acme, globalpay]\n")

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "amb.md", "--force-account", "acme"],
        )

    assert result.exit_code == 0, result.output
    text = (tmp_path / "accounts" / "acme" / "meetings" / "amb.md").read_text()
    assert "ambiguous-match" not in text, "an answered question must stop being reported as open"


def test_force_account_dry_run_moves_nothing(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")

    with patch("fieldkit.config.get_account_names", return_value=["acme"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "target.md", "--force-account", "acme", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "accounts" / "unknown" / "meetings" / "target.md").is_file()
    assert not (tmp_path / "accounts" / "acme" / "meetings" / "target.md").exists()


def test_force_account_requires_file(tmp_path: Path) -> None:
    """Forcing without naming a file would be the bulk sweep this deliberately is not."""
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")

    with patch("fieldkit.config.get_account_names", return_value=["acme"]):
        result = CliRunner().invoke(route_cli, ["--data-root", str(tmp_path), "--force-account", "acme"])

    assert result.exit_code == 3
    assert "must be used together" in result.output


def test_file_requires_force_account(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")
    result = CliRunner().invoke(route_cli, ["--data-root", str(tmp_path), "--file", "target.md"])

    assert result.exit_code == 3
    assert "must be used together" in result.output


def test_force_account_unknown_slug_exits_3(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")

    with patch("fieldkit.config.get_account_names", return_value=["acme"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "target.md", "--force-account", "nope"],
        )

    assert result.exit_code == 3
    assert "unknown account" in result.output.lower()


def test_force_account_missing_file_exits_3(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")

    with patch("fieldkit.config.get_account_names", return_value=["acme"]):
        result = CliRunner().invoke(
            route_cli,
            ["--data-root", str(tmp_path), "--file", "absent.md", "--force-account", "acme"],
        )

    assert result.exit_code == 3
    assert "not found" in result.output.lower()


def test_force_account_cannot_escape_the_unknown_directory(tmp_path: Path) -> None:
    """A path is reduced to its basename, so --file cannot reach outside unknown/."""
    from fieldkit.commands.ingest.route import cli as route_cli

    _make_unknown_meeting(tmp_path, "target.md")
    outside = tmp_path / "accounts" / "globalpay" / "meetings"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "victim.md").write_text("---\ntitle: X\n---\n\n# X\n", encoding="utf-8")

    with patch("fieldkit.config.get_account_names", return_value=["acme", "globalpay"]):
        result = CliRunner().invoke(
            route_cli,
            [
                "--data-root",
                str(tmp_path),
                "--file",
                "../../globalpay/meetings/victim.md",
                "--force-account",
                "acme",
            ],
        )

    assert result.exit_code == 3
    assert (outside / "victim.md").is_file(), "a file outside unknown/ must not be movable"
