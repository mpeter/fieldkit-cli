"""Fail-closed contracts for offline installed Gmail documentation scenarios."""

import json
import re
import shlex
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.doctor.gmail import check_gmail
from fieldkit.commands.gmail import account_tags
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection, SQLitePublicationError
from scripts import smoke_artifact as smoke

pytestmark = pytest.mark.unit

_GMAIL_RECOVERY_CLAIMS = (
    "Before rebuilding, stop every reader, writer, scheduled job, and other process using the cache. "
    "Create an unused, owner-only backup directory on the same filesystem. Rename the original database, "
    "any adjacent `-wal` and `-shm` files, and its complete sibling publication directory into that directory "
    "as one preserved group. For `gmail.db`, the publication directory is `gmail.db.publication`. "
    "Keep the group private: it contains email data. Same-filesystem renames preserve the original database inode; "
    "copies do not guarantee a writable restoration of a managed publication. Never overwrite an existing backup "
    "or try to rebind its identity.",
    "Retain the group until sync completes and `fieldkit doctor gmail` verifies the replacement. To roll back, "
    "stop all cache users again. Rename the complete replacement group into a different unused private directory "
    "on the same filesystem, then rename the original database, saved sidecars, and complete publication directory "
    "back to their exact original paths. Do not merge groups, restore only the database, or hand-edit identities. "
    "Verify with doctor before restarting users.",
    "Use `import-cache` only for a supported legacy Gmail schema, not a managed backup. A managed database "
    "contains `_fieldkit_publication` and is rejected as an import source. Restore a managed backup by the "
    "same-filesystem group rename described above instead.",
    "This resolves the configured `gmail_db` path. Pass `--db PATH` to inspect a different cache. Exit `0` means "
    "the verified managed snapshot is query-ready and has the required tables. Exit `1` means an active writer, "
    "a resource limit, or a not-ready cache makes the check retryable; wait for sync to finish or resolve the "
    "reported resource problem. Exit `3` means invalid or unverified local data needs attention. This check "
    "does not establish mailbox freshness or perform a complete SQLite integrity check. Run `fieldkit gmail sync` "
    "when you need to refresh cached messages.",
)


def _assert_gmail_documentation_recovery(document: str) -> None:
    paragraphs = {" ".join(paragraph.split()) for paragraph in document.split("\n\n")}
    for claim in _GMAIL_RECOVERY_CLAIMS:
        assert claim in paragraphs, f"Unapproved Gmail recovery semantics: {claim}"


def test_documentation_gmail_recovery_claims() -> None:
    _assert_gmail_documentation_recovery(Path("docs/guides/gmail.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Rename the original database", "Copy the original database"),
        ("and its complete sibling publication directory", "without its sibling publication directory"),
        ("copies do not guarantee", "copies guarantee"),
        ("back to their exact original paths", "back to any convenient paths"),
        ("and is rejected as an import source", "and is accepted as an import source"),
        ("Exit `1` means an active writer", "Exit `3` means an active writer"),
    ],
)
def test_documentation_gmail_rejects_false_recovery_semantics(before: str, after: str) -> None:
    document = "\n\n".join(
        " ".join(paragraph.split())
        for paragraph in Path("docs/guides/gmail.md").read_text(encoding="utf-8").split("\n\n")
    )
    assert before in document
    with pytest.raises(AssertionError, match="Gmail recovery semantics"):
        _assert_gmail_documentation_recovery(document.replace(before, after))


def test_documentation_gmail_rejects_prefixed_recovery_negation() -> None:
    document = "\n\n".join(_GMAIL_RECOVERY_CLAIMS)
    with pytest.raises(AssertionError, match="Gmail recovery semantics"):
        _assert_gmail_documentation_recovery(
            document.replace(_GMAIL_RECOVERY_CLAIMS[0], "It is false that " + _GMAIL_RECOVERY_CLAIMS[0])
        )


def _mark_documentation_cache_ready(connection: SQLiteMutationConnection) -> None:
    connection.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')", (GMAIL_QUERY_READY_KEY,))


def test_documentation_gmail_same_filesystem_group_rollback_preserves_writer_identity(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"
    publication = publication_root_for(database)
    receipt = initialize_gmail_publication(database)
    assert receipt.generation == 1
    original = apply_gmail_page(database, _mark_documentation_cache_ready)
    original_inode = database.stat().st_ino
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    database.rename(backup / database.name)
    publication.rename(backup / publication.name)

    replacement = initialize_gmail_publication(database)
    assert replacement.database_uuid != original.database_uuid
    replacement_group = tmp_path / "replacement"
    replacement_group.mkdir(mode=0o700)
    database.rename(replacement_group / database.name)
    publication.rename(replacement_group / publication.name)
    (backup / database.name).rename(database)
    (backup / publication.name).rename(publication)

    restored = apply_gmail_page(database, _mark_documentation_cache_ready)

    assert restored.generation == original.generation + 1
    assert restored.database_uuid == original.database_uuid
    assert database.stat().st_ino == original_inode
    assert check_gmail(database).exit_code == 0


def test_documentation_gmail_copied_database_cannot_rebind_original_publication(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"
    receipt = initialize_gmail_publication(database)
    assert receipt.generation == 1
    publication = publication_root_for(database)
    before = {name: (publication / name).read_bytes() for name in ("identity.json", "receipt.json", "state.json")}
    original = tmp_path / "original.db"
    database.rename(original)
    shutil.copyfile(original, database)
    database.chmod(0o600)
    assert database.stat().st_ino != original.stat().st_ino

    with pytest.raises(SQLitePublicationError, match="identity does not match") as caught:
        apply_gmail_page(database, _mark_documentation_cache_ready)

    assert caught.value.reason == "unverified"
    assert {name: (publication / name).read_bytes() for name in before} == before
    assert database.read_bytes() == original.read_bytes()


def test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"
    receipt = initialize_gmail_publication(database)
    assert receipt.generation == 1

    not_ready = check_gmail(database)

    assert not_ready.exit_code == 1
    assert not not_ready.healthy
    ready_receipt = apply_gmail_page(database, _mark_documentation_cache_ready)
    assert ready_receipt.generation == receipt.generation + 1
    ready = check_gmail(database)
    assert ready.exit_code == 0
    assert ready.healthy


@pytest.fixture
def documentation_legacy_cache(tmp_path: Path) -> Path:
    database = tmp_path / "legacy-gmail.db"
    schema = Path("src/fieldkit/gmail/schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO threads(thread_id, subject) VALUES ('thread-1', 'Fictional planning')")
        connection.execute(
            "INSERT INTO messages(message_id, thread_id, from_addr, to_addr, body_plain) VALUES (?, ?, ?, ?, ?)",
            ("message-1", "thread-1", "alice@example.com", "bob@acme-corp.example.com", "Fictional planning fixture"),
        )
    return database


def test_documentation_import_executes_fixed_legacy_cache_example(
    tmp_path: Path,
    documentation_legacy_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = Path("docs/guides/gmail.md").read_text(encoding="utf-8")
    section = document.split("## Import a supported legacy cache\n", 1)[1]
    blocks = re.findall(r"```console\n(.*?)```", section, flags=re.DOTALL)
    assert blocks == [
        "fieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db --dry-run\n"
        "fieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db\n"
        "fieldkit doctor gmail --db ./managed/gmail.db\n"
    ]
    commands = [shlex.split(line)[1:] for line in blocks[0].splitlines()]
    (tmp_path / "managed").mkdir()
    target = tmp_path / "managed" / "gmail.db"
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    directories = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()}
    monkeypatch.chdir(tmp_path)

    preview_exit = main(commands[0])

    assert preview_exit == 0
    assert "preview passed without creating managed data" in capsys.readouterr().out
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()} == directories

    import_exit = main(commands[1])

    assert import_exit == 0
    assert "Imported Gmail cache as managed generation" in capsys.readouterr().out
    assert documentation_legacy_cache.read_bytes() == before[documentation_legacy_cache.relative_to(tmp_path)]
    with closing(open_gmail_publication(target)) as connection:
        assert [tuple(row) for row in connection.execute("SELECT message_id, body_plain FROM messages")] == [
            ("message-1", "Fictional planning fixture")
        ]
        assert (
            connection.execute("SELECT value FROM sync_state WHERE key = ?", (GMAIL_QUERY_READY_KEY,)).fetchone()[0]
            == "true"
        )

    doctor_paths = {path.relative_to(target.parent) for path in target.parent.rglob("*")}
    doctor_files = {
        path.relative_to(target.parent): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_mode,
            path.stat().st_ino,
        )
        for path in target.parent.rglob("*")
        if path.is_file()
    }
    doctor_directories = {
        path.relative_to(target.parent): (path.stat().st_mode, path.stat().st_ino)
        for path in target.parent.rglob("*")
        if path.is_dir()
    }

    doctor_exit = main(commands[2])

    assert doctor_exit == 0
    assert "1 messages" in capsys.readouterr().out
    assert {path.relative_to(target.parent) for path in target.parent.rglob("*")} == doctor_paths
    assert {
        path.relative_to(target.parent): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_mode,
            path.stat().st_ino,
        )
        for path in target.parent.rglob("*")
        if path.is_file()
    } == doctor_files
    assert {
        path.relative_to(target.parent): (path.stat().st_mode, path.stat().st_ino)
        for path in target.parent.rglob("*")
        if path.is_dir()
    } == doctor_directories
    assert documentation_legacy_cache.read_bytes() == before[documentation_legacy_cache.relative_to(tmp_path)]
    assert not Path(f"{documentation_legacy_cache}-wal").exists()
    assert not Path(f"{documentation_legacy_cache}-shm").exists()


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("occupied", ["database", "publication"])
def test_documentation_import_rejects_occupied_destination_without_writes(
    tmp_path: Path,
    documentation_legacy_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    occupied: str,
) -> None:
    target = tmp_path / "managed" / "gmail.db"
    target.parent.mkdir()
    if occupied == "database":
        target.write_bytes(b"Preserve the existing destination")
    else:
        publication_root_for(target).mkdir(mode=0o700)
        (publication_root_for(target) / "preserve.txt").write_text(
            "Preserve the existing publication", encoding="utf-8"
        )
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    directories = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()}
    monkeypatch.chdir(tmp_path)
    argv = ["gmail", "import-cache", "--source", "./legacy-gmail.db", "--db", "./managed/gmail.db"]
    if dry_run:
        argv.append("--dry-run")

    exit_code = main(argv)

    assert exit_code == 3
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()} == directories
    assert documentation_legacy_cache.read_bytes() == before[documentation_legacy_cache.relative_to(tmp_path)]


@pytest.mark.parametrize("dry_run", [True, False])
def test_documentation_import_cli_rejects_copied_managed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    database = tmp_path / "gmail.db"
    receipt = initialize_gmail_publication(database)
    assert receipt.generation == 1
    copied = tmp_path / "legacy-gmail.db"
    shutil.copyfile(database, copied)
    (tmp_path / "managed").mkdir()
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.chdir(tmp_path)
    argv = ["gmail", "import-cache", "--source", "./legacy-gmail.db", "--db", "./managed/gmail.db"]
    if dry_run:
        argv.append("--dry-run")

    exit_code = main(argv)

    assert exit_code == 3
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "managed" / "gmail.db").exists()
    assert not (tmp_path / "managed" / "gmail.db.publication").exists()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "exit",
        "write",
        "sibling-write",
        "guidance",
        "unsafe",
        "missing-backup",
        "missing-permissions",
        "missing-table-name",
        "missing-sync",
        "missing-recoverable",
        "missing-stop",
        "missing-people",
        "missing-local-sync",
        "missing-path",
    ],
)
def test_gmail_recovery_requires_data_exit_and_preserved_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    calls: list[tuple[list[str], str]] = []

    def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env["PYTHONPATH"] == "network-guard"
        calls.append((argv, env["FIELDKIT_DATA_DIR"]))
        database = Path(env["FIELDKIT_DATA_DIR"]) / "gmail.db"
        assert database.parent.parent == tmp_path / "recovery"
        assert env["XDG_CONFIG_HOME"] == str(database.parent / "config")
        config = Path(env["XDG_CONFIG_HOME"]) / "fieldkit" / "config.yaml"
        assert json.loads(config.read_text(encoding="utf-8"))["gmail_db"] == str(database)
        if failure == "write":
            database.write_bytes(b"changed")
        if failure == "sibling-write":
            (database.parent.parent / f"unexpected-{len(calls)}").write_bytes(b"changed")
        guidance = (
            f"{database}: preserve a backup; recoverable backup; check path and permissions; "
            "stop cache users; people; thread_accounts; fieldkit gmail sync; fieldkit sync"
        )
        omitted_phrase = {
            "missing-backup": "preserve a backup",
            "missing-permissions": "check path and permissions",
            "missing-table-name": "thread_accounts",
            "missing-sync": "fieldkit gmail sync",
            "missing-recoverable": "recoverable backup",
            "missing-stop": "stop cache users",
            "missing-people": "people",
            "missing-local-sync": "fieldkit sync",
            "missing-path": str(database),
        }.get(failure or "")
        if omitted_phrase is not None:
            guidance = guidance.replace(omitted_phrase, "")
        return subprocess.CompletedProcess(
            argv,
            0 if failure == "exit" else 3,
            "wrong" if failure == "guidance" else guidance + (" delete cache" if failure == "unsafe" else ""),
            "",
        )

    monkeypatch.setattr(smoke, "_run", run)
    criteria = smoke._gmail_recovery_examples(
        Path("installed-fieldkit"), root=tmp_path / "recovery", cwd=tmp_path, env={"PYTHONPATH": "network-guard"}
    )
    assert len(criteria) == 4
    expected_failures = {
        None: set(),
        "missing-backup": {"SMOKE146"},
        "missing-permissions": {"SMOKE147"},
        "missing-table-name": {"SMOKE148"},
        "missing-sync": {"SMOKE145", "SMOKE146", "SMOKE147"},
        "missing-recoverable": {"SMOKE147"},
        "missing-stop": {"SMOKE147"},
        "missing-people": {"SMOKE148"},
        "missing-local-sync": {"SMOKE148"},
        "missing-path": {"SMOKE147"},
    }.get(failure, {"SMOKE145", "SMOKE146", "SMOKE147", "SMOKE148"})
    assert {item.criterion_id for item in criteria if item.status != "pass"} == expected_failures
    assert calls == [
        (["installed-fieldkit", "doctor", "gmail"], str(tmp_path / "recovery" / state))
        for state in ("missing", "empty", "corrupt", "missing-table")
    ]


def test_documentation_account_tag_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = Path("docs/guides/gmail.md").read_text(encoding="utf-8")
    section = document.split("**Tag threads by account:**", 1)[1].split("**Generate Gmail intelligence reports:**", 1)[
        0
    ]
    blocks = re.findall(r"```[^\n]*\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == 2
    assert blocks[0].strip() == "fieldkit gmail account-tags"
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)

    def seed(connection: SQLiteMutationConnection) -> None:
        for number, account in ((1, "acme-corp"), (2, "acme-corp"), (3, "global-pay")):
            connection.execute("INSERT INTO threads(thread_id) VALUES (?)", (str(number),))
            connection.execute(
                "INSERT INTO messages(message_id,thread_id,labels) VALUES (?,?,?)",
                (str(number), str(number), json.dumps([account])),
            )
            connection.execute(
                "INSERT OR IGNORE INTO labels(label_id,label_name) VALUES (?,?)", (account, f"ref/{account}")
            )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(database, seed)
    monkeypatch.setattr(account_tags, "get_gmail_db_path", lambda: database)
    result = CliRunner().invoke(account_tags.cli, [])

    assert result.exit_code == 0, result.output
    assert result.output == blocks[1]
    with open_gmail_publication(database) as connection:
        rows = connection.execute("SELECT thread_id,account FROM thread_accounts ORDER BY thread_id").fetchall()
        assert [tuple(row) for row in rows] == [
            ("1", "acme-corp"),
            ("2", "acme-corp"),
            ("3", "global-pay"),
        ]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "setup",
        "setup-path",
        "setup-missing",
        "tags-output",
        "tags-rows",
        "doctor",
        "report",
        "report-data",
        "report-match",
        "report-count",
        "pursuit-write",
        "pursuit-add",
    ],
)
def test_gmail_examples_require_outputs_and_persisted_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    workspace = tmp_path / "workspace"
    pursuit = workspace / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"
    pursuit.parent.mkdir(parents=True)
    pursuit.write_text("---\nstage: discover\n---\n", encoding="utf-8")
    database = workspace / "data" / "gmail.db"
    database.parent.mkdir()
    calls: list[list[str]] = []

    def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env["PYTHONPATH"] == "network-guard"
        assert timeout == smoke.COMMAND_TIMEOUT_SECONDS
        calls.append(argv)
        if argv[1] == "-c":
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE thread_accounts(thread_id TEXT, account TEXT)")
            output_path = database
            if failure == "setup-path":
                output_path = workspace / "other.db"
                output_path.write_bytes(database.read_bytes())
            elif failure == "setup-missing":
                output_path = workspace / "missing.db"
            return subprocess.CompletedProcess(argv, 3 if failure == "setup" else 0, str(output_path), "")
        command = argv[1:]
        if command == ["gmail", "account-tags"]:
            with sqlite3.connect(database) as connection:
                if failure != "tags-rows":
                    connection.executemany(
                        "INSERT INTO thread_accounts VALUES (?, ?)",
                        [("thread-1", "acme-corp"), ("thread-2", "acme-corp"), ("thread-3", "global-pay")],
                    )
            output = (
                "Upserted 3 thread-account associations across 2 account(s):\n"
                "  acme-corp: 2 threads\n  global-pay: 1 threads\n"
            )
            return subprocess.CompletedProcess(argv, 0, "wrong" if failure == "tags-output" else output, "")
        if command == ["doctor", "gmail"]:
            return subprocess.CompletedProcess(
                argv, 3 if failure == "doctor" else 0, "gmail: OK — 3 messages, 0.1 MB", ""
            )
        assert command == ["gmail", "enrich-pursuits"]
        if failure != "report":
            report = pursuit.parents[1] / "gmail-intel.md"
            report.write_text(
                "# Gmail Intelligence Report: acme-corp\n## Top Contacts by Email Volume\n"
                + (
                    ""
                    if failure == "report-data"
                    else f"| contact@example.com | {0 if failure == 'report-count' else 2} | 0d ago |\n"
                )
                + "## Champion Signals (Top 10 Contacts)\n## Per-Pursuit Thread Matches\n"
                + (
                    ""
                    if failure == "report-match"
                    else "### acme-corp-q3\nAcme quarterly planning contact@example.com\n"
                )
                + "## Review Checklist\n",
                encoding="utf-8",
            )
        if failure == "pursuit-write":
            pursuit.write_text("changed", encoding="utf-8")
        if failure == "pursuit-add":
            pursuit.with_name("unexpected.md").write_text("unexpected", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "Done.", "")

    monkeypatch.setattr(smoke, "_run", run)
    criteria = smoke._gmail_examples(
        Path("fieldkit"), python=Path("python"), workspace=workspace, cwd=tmp_path, env={"PYTHONPATH": "network-guard"}
    )

    assert len(criteria) == 4
    assert calls[0] == ["python", "-c", smoke.GMAIL_SETUP_PROGRAM]
    expected = {
        None: set(),
        "setup": {"SMOKE141"},
        "setup-path": {"SMOKE141", "SMOKE142"},
        "setup-missing": {"SMOKE141", "SMOKE142"},
        "tags-output": {"SMOKE142"},
        "tags-rows": {"SMOKE142"},
        "doctor": {"SMOKE143"},
        "report": {"SMOKE144"},
        "report-data": {"SMOKE144"},
        "report-match": {"SMOKE144"},
        "report-count": {"SMOKE144"},
        "pursuit-write": {"SMOKE144"},
        "pursuit-add": {"SMOKE144"},
    }
    assert {item.criterion_id for item in criteria if item.status != "pass"} == expected[failure]
    assert [argv[1:] for argv in calls[1:]] == [
        ["gmail", "account-tags"],
        ["doctor", "gmail"],
        ["gmail", "enrich-pursuits"],
    ]
