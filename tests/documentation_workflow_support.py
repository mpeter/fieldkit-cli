"""Shared fictional fixtures for executable documentation workflow scenarios."""

from __future__ import annotations

import socket
import stat
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
import yaml

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.__main__ import main
from fieldkit.config import clear_config_caches
from fieldkit.gmail.discover import clear_gmail_caches
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
from fieldkit.sqlite_publication import SQLiteMutationConnection

DOCUMENTED_DATE = "2026-06-15"


DOCUMENTED_EPOCH = int(datetime(2026, 6, 15, tzinfo=UTC).timestamp())


def snapshot_workflow(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    """Capture names, types, mtimes, and bytes so read scenarios cannot hide writes."""
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            path.lstat().st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(path.lstat().st_mode) else None,
        )
        for path in sorted(root.rglob("*"))
    }


@dataclass(frozen=True)
class WorkflowExecution:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def invoke_workflow(argv: list[str]) -> WorkflowExecution:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return WorkflowExecution(exit_code=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


@pytest.fixture
def deny_documentation_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny DNS and every socket destination during local documentation scenarios."""

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("documentation scenario attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket.socket, "sendto", reject_network)


@pytest.fixture
def documented_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deny_documentation_network: None
) -> Iterator[Path]:
    workspace = tmp_path / "workspace"
    (workspace / "accounts" / "acme-corp" / "projects").mkdir(parents=True)
    (workspace / "accounts" / "acme-corp" / "pursuits").mkdir(parents=True)
    (workspace / "config").mkdir()
    (workspace / "data").mkdir()

    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    xdg_config = tmp_path / "config"
    home.mkdir()
    runtime.mkdir()
    config_path = xdg_config / "fieldkit" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "fieldkit_home": str(workspace),
                "gmail_db": str(workspace / "data" / "gmail.db"),
                "email": "seller@example.com",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (workspace / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "internal_domains": ["example.com"],
                "salesforce": {"org_url": "https://acme-example.my.salesforce.com"},
                "accounts": {
                    "acme-corp": {
                        "domains": ["acme-corp.example.com"],
                        "blindspots_min_messages": 1,
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(runtime))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    clear_config_caches()
    clear_gmail_caches()
    yield workspace
    clear_config_caches()
    clear_gmail_caches()


def write_documented_pursuit_data(workspace: Path) -> None:
    (workspace / "accounts" / "acme-corp" / "projects" / "implementation.md").write_text(
        """---
sf_record_id: "a74Pe000001W8oH"
sf_stage: "In Progress"
sf_contract_start: "2026-01-06"
sf_contract_end: "2027-12-31"
sf_opportunity: "Acme Rollout"
sf_opa_number: "12345"
sf_last_pulled: "2026-05-11"
---
# Acme Rollout
""",
        encoding="utf-8",
    )
    (workspace / "accounts" / "acme-corp" / "pursuits" / "platform.md").write_text(
        """---
stage: discover
gate-status: pending
last-transition: 2026-05-10
transition-history:
  - date: 2026-05-10
    from: ""
    to: discover
    gate-result: pass
    override-reason: ""
meddpicc:
  metrics: 2
  economic-buyer: 2
  decision-criteria: 2
  decision-process: 2
  identify-pain: 2
  champion: 2
  competition: 2
  paper-process: 2
sf_opportunity_id: "006Pe000012n2GkIAI"
sf_stage: Discover
sf_close_date: 12/31/2027
sf_arr: $100,000.00
sf_owner: Jane Doe
sf_next_steps: Schedule discovery call
sf_last_pulled: 2026-05-14T17:35:36Z
---
# Acme Platform

Fictional pursuit notes.
""",
        encoding="utf-8",
    )


def write_second_documented_account(workspace: Path) -> None:
    """Add distinct report data and a real cross-account affiliation fixture."""
    source = workspace / "accounts" / "acme-corp"
    target = workspace / "accounts" / "beta-corp"
    for directory, filename in (("projects", "implementation.md"), ("pursuits", "platform.md")):
        destination = target / directory / filename
        destination.parent.mkdir(parents=True)
        text = (source / directory / filename).read_text(encoding="utf-8")
        destination.write_text(text.replace("Acme", "Beta").replace("$100,000.00", "$200,000.00"), encoding="utf-8")
    account_config = workspace / "config" / "accounts.yaml"
    payload = yaml.safe_load(account_config.read_text(encoding="utf-8"))
    payload["accounts"]["beta-corp"] = {"domains": ["beta-corp.example.com"], "blindspots_min_messages": 1}
    account_config.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    affiliation = (
        "\n| Name | Title | Support | MEDDPICC Role | Notes |\n"
        "|---|---|---|---|---|\n"
        "| Alex Example | Engineer | Unknown | Unknown | alex@acme-corp.example.com |\n"
    )
    for account in (source, target):
        pursuit = account / "pursuits" / "platform.md"
        pursuit.write_text(pursuit.read_text(encoding="utf-8") + affiliation, encoding="utf-8")
    clear_config_caches()


def prepare_documented_gmail_cache(workspace: Path) -> None:
    database = workspace / "data" / "gmail.db"
    initialize_gmail_publication(database)

    def insert_fixture(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )
        connection.execute(
            "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("thread-1", "Acme planning", "Fictional planning note", 3, DOCUMENTED_DATE),
        )
        for index in range(1, 4):
            connection.execute(
                """INSERT INTO messages(
                       message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                       date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"message-{index}",
                    "thread-1",
                    "alex@acme-corp.example.com",
                    "seller@example.com",
                    "",
                    "Acme planning",
                    DOCUMENTED_DATE,
                    DOCUMENTED_EPOCH + index,
                    '["INBOX"]',
                    "Fictional planning note",
                    "",
                    24,
                    "Fictional planning note",
                ),
            )
        connection.execute(
            """INSERT INTO people(
                   email, display_name, first_seen, last_seen, message_count,
                   thread_count, initiated_count, domain, account, is_internal
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "alex@acme-corp.example.com",
                "Alex Example",
                DOCUMENTED_DATE,
                DOCUMENTED_DATE,
                3,
                1,
                1,
                "acme-corp.example.com",
                "acme-corp",
                0,
            ),
        )
        connection.execute(
            "INSERT INTO thread_accounts(thread_id, account) VALUES (?, ?)",
            ("thread-1", "acme-corp"),
        )

    apply_gmail_page(database, insert_fixture)
