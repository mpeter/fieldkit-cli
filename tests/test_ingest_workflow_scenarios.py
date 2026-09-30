"""Executable, local-only scenarios for the public ingest workflows."""

from __future__ import annotations

import json
import socket
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fieldkit.gmail.publication import apply_gmail_page
from fieldkit.sqlite_publication import SQLiteMutationConnection
from scripts.check_documentation_contract import fenced_blocks
from tests.documentation_workflow_support import (
    invoke_workflow,
    prepare_documented_gmail_cache,
    snapshot_workflow,
    write_documented_pursuit_data,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INGEST_SKILL = _REPO_ROOT / "src/fieldkit/skills/ingest/SKILL.md"
_GMAIL_REFRESH = _REPO_ROOT / "src/fieldkit/skills/ingest/ops/gmail-refresh.md"


def _commands(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for block in fenced_blocks(path)
        if block.language in {"bash", "console", "sh"}
        for line in block.body.splitlines()
        if line.strip()
    )


def _publish_ingest_candidates(workspace: Path) -> None:
    prepare_documented_gmail_cache(workspace)
    database = workspace / "data/gmail.db"
    recent_epoch = int((datetime.now(UTC) - timedelta(days=1)).timestamp())

    def insert_candidates(connection: SQLiteMutationConnection) -> None:
        fixtures = (
            (
                "thread-acme-notes",
                "message-acme-notes",
                "ACME_DOC_2026",
                'Notes: "Acme planning" September 27, 2026',
                "team@acme-corp.example.com",
                '["INBOX", "label-ref-acme"]',
            ),
            (
                "thread-other-notes",
                "message-other-notes",
                "OTHER_DOC_2026",
                'Notes: "Other planning" September 27, 2026',
                "team@other.example.net",
                '["INBOX"]',
            ),
        )
        for thread_id, message_id, doc_id, subject, recipient, labels in fixtures:
            document_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            connection.execute(
                "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?, ?, ?, 1, ?)",
                (thread_id, subject, "Fictional meeting notes", "2026-09-27"),
            )
            connection.execute(
                """INSERT INTO messages(
                       message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                       date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    thread_id,
                    "Gemini <gemini-notes@google.com>",
                    recipient,
                    "",
                    subject,
                    "2026-09-27",
                    recent_epoch,
                    labels,
                    f"Meeting notes: {document_url}",
                    "",
                    64,
                    "Fictional meeting notes",
                ),
            )
        connection.execute("INSERT INTO labels(label_id, label_name) VALUES ('label-ref-acme', 'ref/acme-corp')")

    apply_gmail_page(database, insert_candidates)


def _create_legacy_cache(path: Path) -> None:
    schema = (_REPO_ROOT / "src/fieldkit/gmail/schema.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        connection.execute("INSERT INTO sync_state(key, value) VALUES ('fieldkit_query_ready', 'true')")
        connection.execute(
            "INSERT INTO threads(thread_id, subject, message_count) VALUES ('legacy-thread', 'Legacy fixture', 0)"
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


@pytest.mark.usefixtures("deny_documentation_network")
def test_documentation_network_guard_denies_dns_stream_and_datagram_calls() -> None:
    with pytest.raises(AssertionError, match="documentation scenario attempted network access"):
        socket.getaddrinfo("localhost", 443)
    with (
        socket.socket() as stream,
        pytest.raises(AssertionError, match="documentation scenario attempted network access"),
    ):
        stream.connect_ex(("127.0.0.1", 443))
    with (
        socket.socket() as stream,
        pytest.raises(AssertionError, match="documentation scenario attempted network access"),
    ):
        stream.connect(("127.0.0.1", 443))
    with (
        socket.socket(type=socket.SOCK_DGRAM) as datagram,
        pytest.raises(AssertionError, match="documentation scenario attempted network access"),
    ):
        datagram.sendto(b"probe", ("127.0.0.1", 443))


@pytest.mark.usefixtures("deny_documentation_network")
def test_documented_status_and_discovery_previews_are_fixed_argv_and_read_only(
    documented_workspace: Path,
) -> None:
    _publish_ingest_candidates(documented_workspace)
    trial_root = documented_workspace.parent
    before = snapshot_workflow(trial_root)

    status = invoke_workflow(["ingest", "status", "--account", "acme-corp", "--json"])
    scoped = invoke_workflow(
        [
            "ingest",
            "discover",
            "--pipeline",
            "transcript-ingest",
            "--dry-run",
            "--account",
            "acme-corp",
            "--json",
        ]
    )
    global_preview = invoke_workflow(
        [
            "ingest",
            "discover",
            "--pipeline",
            "transcript-ingest",
            "--dry-run",
            "--limit",
            "2",
            "--json",
        ]
    )

    status_payload = json.loads(status.stdout)
    scoped_payload = json.loads(scoped.stdout)
    global_payload = json.loads(global_preview.stdout)
    assert (status.exit_code, scoped.exit_code, global_preview.exit_code) == (0, 0, 0)
    assert status_payload["filters"] == {"account": "acme-corp"}
    assert status_payload["db_connected"] is False
    assert [(item["source_id"], item["subject"]) for item in scoped_payload["items"]] == [
        ("ACME_DOC_2026", 'Notes: "Acme planning" September 27, 2026')
    ]
    assert scoped_payload["filters"] == {"account": "acme-corp", "limit": None}
    assert {item["source_id"] for item in global_payload["items"]} == {
        "ACME_DOC_2026",
        "OTHER_DOC_2026",
    }
    assert global_payload["filters"] == {"account": None, "limit": 2}
    assert snapshot_workflow(trial_root) == before


@pytest.mark.usefixtures("deny_documentation_network")
def test_documented_global_discovery_writes_only_the_bounded_registry(
    documented_workspace: Path,
) -> None:
    _publish_ingest_candidates(documented_workspace)
    trial_root = documented_workspace.parent
    before = snapshot_workflow(trial_root)

    result = invoke_workflow(["ingest", "discover", "--pipeline", "transcript-ingest", "--limit", "2", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.stderr
    assert payload["newly_discovered"] == 2
    assert payload["total_sources"] == 2
    assert {item["source_id"] for item in payload["items"]} == {
        "ACME_DOC_2026",
        "OTHER_DOC_2026",
    }
    assert payload["filters"] == {"account": None, "limit": 2}
    after = snapshot_workflow(trial_root)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    assert changed == {"runtime", "runtime/pipeline.db"}


@pytest.mark.usefixtures("deny_documentation_network")
def test_documented_backfill_reports_missing_provenance_without_writing(
    documented_workspace: Path,
) -> None:
    meetings = documented_workspace / "accounts/acme-corp/meetings"
    meetings.mkdir()
    missing = meetings / "2026-09-27-acme-planning.md"
    missing.write_text("---\naccount: acme-corp\n---\n# Acme planning\n", encoding="utf-8")
    (meetings / "2026-09-26-tracked.md").write_text(
        "---\naccount: acme-corp\nsource_id: ACME_TRACKED\n---\n# Tracked\n",
        encoding="utf-8",
    )
    trial_root = documented_workspace.parent
    before = snapshot_workflow(trial_root)

    result = invoke_workflow(["ingest", "backfill", "--account", "acme-corp", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.stderr
    assert payload["count"] == 1
    assert payload["scanned"] == 2
    assert payload["items"] == [
        {
            "path": str(missing),
            "account": "acme-corp",
            "reason": "frontmatter present but missing source_id",
        }
    ]
    assert snapshot_workflow(trial_root) == before


@pytest.mark.usefixtures("deny_documentation_network")
def test_cached_gmail_refresh_uses_one_published_generation_and_scoped_report(
    documented_workspace: Path,
) -> None:
    _publish_ingest_candidates(documented_workspace)
    write_documented_pursuit_data(documented_workspace)
    trial_root = documented_workspace.parent
    before_doctor = snapshot_workflow(trial_root)

    doctor = invoke_workflow(["doctor", "gmail", "--json"])
    assert doctor.exit_code == 0, doctor.stderr
    assert json.loads(doctor.stdout)["healthy"] is True
    assert snapshot_workflow(trial_root) == before_doctor

    tags = invoke_workflow(["gmail", "account-tags", "--account", "acme-corp", "--json"])
    enrich = invoke_workflow(["gmail", "enrich-pursuits", "--account", "acme-corp", "--json"])

    tag_payload = json.loads(tags.stdout)
    enrich_payload = json.loads(enrich.stdout)
    report = documented_workspace / "accounts/acme-corp/gmail-intel.md"
    assert (tags.exit_code, enrich.exit_code) == (0, 0)
    assert tag_payload["filters"] == {"account": "acme-corp"}
    assert tag_payload["associations"] == 1
    assert enrich_payload["requested"] == "acme-corp"
    assert enrich_payload["error"] is None
    assert enrich_payload["accounts"] == [{"account": "acme-corp", "path": str(report), "status": "written"}]
    assert report.is_file()
    report_text = report.read_text(encoding="utf-8")
    assert "# Gmail Intelligence Report: acme-corp" in report_text
    assert "team@acme-corp.example.com | 1 |" in report_text
    assert "team@other.example.net" not in report_text
    assert not (trial_root / "home/.config").exists()


@pytest.mark.usefixtures("deny_documentation_network")
def test_legacy_import_requires_a_fresh_scoped_target_and_preserves_source(
    documented_workspace: Path,
) -> None:
    trial_root = documented_workspace.parent
    source = trial_root / "legacy-gmail.db"
    target = trial_root / "runtime/imported-gmail.db"
    _create_legacy_cache(source)
    source_before = snapshot_workflow(trial_root)[source.name]

    first = invoke_workflow(["gmail", "import-cache", "--source", str(source), "--db", str(target), "--json"])
    first_payload = json.loads(first.stdout)
    after_first = snapshot_workflow(trial_root)
    second = invoke_workflow(["gmail", "import-cache", "--source", str(source), "--db", str(target), "--json"])

    assert first.exit_code == 0, first.stderr
    assert first_payload["status"] == "ok"
    assert first_payload["table_counts"]["threads"] == 1
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert after_first[source.name] == source_before
    assert target.exists()
    assert second.exit_code == 3
    assert second.stdout == ""
    assert second.stderr == "[cli_exit] SQLite snapshot could not be verified — inspect the database before retrying.\n"
    assert snapshot_workflow(trial_root) == after_first


@pytest.mark.usefixtures("deny_documentation_network")
def test_cached_refresh_stops_at_an_unready_prerequisite_without_later_writes(
    documented_workspace: Path,
) -> None:
    trial_root = documented_workspace.parent
    before = snapshot_workflow(trial_root)

    doctor = invoke_workflow(["doctor", "gmail", "--json"])

    payload = json.loads(doctor.stdout)
    assert doctor.exit_code == 3
    assert payload["healthy"] is False
    assert payload["configured"] is False
    assert not (documented_workspace / "accounts/acme-corp/gmail-intel.md").exists()
    assert snapshot_workflow(trial_root) == before


def test_safe_ingest_scenarios_bind_the_exact_documented_commands() -> None:
    ingest_commands = _commands(_INGEST_SKILL)
    refresh_commands = _commands(_GMAIL_REFRESH)

    assert ingest_commands == (
        "fieldkit ingest status --account <account> --json",
        "fieldkit ingest discover --pipeline transcript-ingest --dry-run --account <account> --json",
        "fieldkit ingest discover --pipeline transcript-ingest --dry-run --limit <N> --json",
        "fieldkit ingest discover --pipeline transcript-ingest --limit <N> --json",
        "fieldkit doctor google --json",
        "fieldkit ingest run --pipeline transcript-ingest --interactive",
        "fieldkit ingest backfill --account <account> --json",
    )
    assert refresh_commands == (
        "fieldkit doctor google --json",
        "fieldkit gmail sync --since <YYYY-MM-DD> --max-messages <N> --json",
        "fieldkit gmail account-tags --account <account> --json",
        "fieldkit gmail enrich-pursuits --account <account> --json",
        "fieldkit doctor gmail --json",
        "fieldkit gmail account-tags --account <account> --json",
        "fieldkit gmail enrich-pursuits --account <account> --json",
        "fieldkit gmail import-cache --source <legacy-gmail.db> --db <fresh-managed-gmail.db> --json",
    )
