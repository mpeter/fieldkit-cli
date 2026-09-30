"""Real local ingest effects with fictional Google Docs provider responses."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import fieldkit.__main__ as main_module
import fieldkit.config as config
import fieldkit.llm.core as llm_core
from fieldkit.config import clear_config_caches
from fieldkit.gmail.publication import apply_gmail_page
from fieldkit.ingest.db import get_db_path, get_db_read_only
from fieldkit.ingest.pipeline import stage1_clean, stage2_extract
from fieldkit.ingest.writeback import parse_meeting_frontmatter
from fieldkit.sqlite_publication import SQLiteMutationConnection
from tests.documentation_workflow_support import (
    invoke_workflow,
    prepare_documented_gmail_cache,
    snapshot_workflow,
    write_documented_pursuit_data,
    write_second_documented_account,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("deny_documentation_network")]

_SOURCE_ID = "FICTIONAL_APPROVAL_DOC"
_TITLE = "Acme platform planning"
_DATE = "2026-09-27"
_RAW = (
    "Seller: Um, the platform rollout needs a proposal. Alex: Please send the proposal "
    "after procurement approves the budget. We agreed to review the technical requirements next week."
)


@pytest.fixture
def approval_workspace(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Use managed Gmail publication, configured roots, and no real dotenv input."""
    monkeypatch.setattr(main_module, "load_dotenv_safe", lambda: None)
    write_documented_pursuit_data(documented_workspace)
    write_second_documented_account(documented_workspace)
    account_config = documented_workspace / "config/accounts.yaml"
    accounts = yaml.safe_load(account_config.read_text(encoding="utf-8"))
    for account in ("acme-corp", "beta-corp"):
        accounts["accounts"][account]["pursuit_dir"] = f"accounts/{account}/pursuits"
        accounts["accounts"][account]["keywords"] = ["platform"]
    account_config.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    user_config = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    user_config["name"] = "Seller Example"
    config.CONFIG_PATH.write_text(yaml.safe_dump(user_config), encoding="utf-8")
    monkeypatch.setattr("fieldkit.ingest.db.CONFIG_PATH", config.CONFIG_PATH)
    prepare_documented_gmail_cache(documented_workspace)
    epoch = int(datetime.now(UTC).timestamp())

    def publish(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?, ?, ?, 1, ?)",
            ("approval-thread", f'Notes: "{_TITLE}" September 27, 2026', "Fictional notes", _DATE),
        )
        connection.execute(
            """INSERT INTO messages(
                   message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                   date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "approval-message",
                "approval-thread",
                "Gemini <gemini-notes@google.com>",
                "alex@acme-corp.example.com",
                "",
                f'Notes: "{_TITLE}" September 27, 2026',
                _DATE,
                epoch,
                '["INBOX"]',
                f"https://docs.google.com/document/d/{_SOURCE_ID}/edit",
                "",
                64,
                "Fictional notes",
            ),
        )

    apply_gmail_page(documented_workspace / "data/gmail.db", publish)
    clear_config_caches()
    yield documented_workspace
    clear_config_caches()


def _docs_provider(monkeypatch: pytest.MonkeyPatch, *, account: str = "acme-corp") -> MagicMock:
    """Fixture only the external Docs API response; retain the real tab parser."""
    service = MagicMock()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": _TITLE,
        "tabs": [
            {
                "tabProperties": {"title": title},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {"textRun": {"content": text}},
                                    ]
                                }
                            }
                        ]
                    }
                },
            }
            for title, text in (
                ("Notes", f"Invited: alex@{account}.com, seller@example.com\n\nNext Steps\n- Send proposal\n"),
                ("Transcript", _RAW),
            )
        ],
    }
    monkeypatch.setattr("fieldkit.ingest.docs.get_docs_service", lambda: service)
    return service


def _forbid_provider_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def forbid(command: str, *_args: object, **_kwargs: object) -> None:
        attempts.append(command)
        raise AssertionError("no-LLM ingest attempted provider setup")

    monkeypatch.setattr(llm_core, "require_optional_profile", forbid)
    return attempts


def _stored_note(workspace: Path) -> Path:
    with closing(get_db_read_only(get_db_path())) as connection:
        source = connection.execute("SELECT status FROM sources WHERE source_id = ?", (_SOURCE_ID,)).fetchone()
        assert source is not None and source["status"] == "processed"
        artifacts = connection.execute("SELECT source_id, content_path FROM artifacts").fetchall()
        assert len(artifacts) == 1
        assert artifacts[0]["source_id"] == _SOURCE_ID
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
    note = Path(artifacts[0]["content_path"])
    assert note.is_relative_to(workspace)
    assert note.is_file()
    return note


def test_full_no_llm_ingest_preserves_raw_input_and_writes_supported_effects(
    approval_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_attempts = _forbid_provider_llm(monkeypatch)
    service = _docs_provider(monkeypatch)
    pursuit = approval_workspace / "accounts/acme-corp/pursuits/platform.md"
    original_pursuit = pursuit.read_bytes()
    beta_before = snapshot_workflow(approval_workspace / "accounts/beta-corp")

    result = invoke_workflow(["ingest", "run", "--pipeline", "transcript-ingest", "--limit", "1", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["completed"] == [_SOURCE_ID]
    assert payload["degraded"] == []
    assert provider_attempts == []
    service.documents.return_value.get.assert_called_once_with(documentId=_SOURCE_ID, includeTabsContent=True)
    service.documents.return_value.get.return_value.execute.assert_called_once_with()
    note = _stored_note(approval_workspace)
    assert note.parent == approval_workspace / "accounts/acme-corp/meetings"
    content = note.read_text(encoding="utf-8")
    frontmatter = parse_meeting_frontmatter(content)
    assert frontmatter["confidence"] == "stub"
    assert frontmatter["source_quality"] == "full"
    for key in ("participants", "action_items", "key_decisions", "key_topics"):
        assert frontmatter[key] == []
    assert _RAW.encode("utf-8") in note.read_bytes()
    assert "## Action Items" not in content
    assert "## Gemini Suggested Next Steps" in content
    assert not (approval_workspace / "TASKS.md").exists()
    assert pursuit.read_bytes() != original_pursuit
    assert f"../meetings/{note.name}" in pursuit.read_text(encoding="utf-8")
    assert "fieldkit-ingest-pursuit:v1:" in pursuit.read_text(encoding="utf-8")
    assert snapshot_workflow(approval_workspace / "accounts/beta-corp") == beta_before


def test_no_llm_extraction_direct_returns_raw_and_empty_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    attempts = _forbid_provider_llm(monkeypatch)
    cleaned = stage1_clean(_RAW)
    assert cleaned.text == _RAW
    assert cleaned.bypassed is False
    extracted = stage2_extract(cleaned)
    assert extracted.confidence == "stub"
    assert extracted.participants == extracted.action_items == extracted.key_decisions == extracted.key_topics == []
    assert attempts == []


def test_interactive_rejection_discloses_identity_and_leaves_source_pending_without_effects(
    approval_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _docs_provider(monkeypatch)
    attempts = _forbid_provider_llm(monkeypatch)
    before = snapshot_workflow(approval_workspace)
    seen: list[str] = []

    def reject(prompt: str) -> str:
        assert prompt == "Process? [y/n/q] "
        assert isinstance(sys.stdout, StringIO)
        visible = sys.stdout.getvalue()
        assert f"[{_DATE}] {_TITLE} ({_SOURCE_ID})" in visible
        assert "Processing [" not in visible
        service.documents.assert_not_called()
        seen.append(visible)
        return "n"

    monkeypatch.setattr("builtins.input", reject)
    result = invoke_workflow(["ingest", "run", "--pipeline", "transcript-ingest", "--interactive", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert f"Skipped: {_SOURCE_ID}" in result.stdout
    assert "0 processed (0 degraded), 1 skipped, 0 error(s)" in result.stdout
    assert snapshot_workflow(approval_workspace) == before
    assert attempts == []
    service.documents.assert_not_called()
    with closing(get_db_read_only(get_db_path())) as connection:
        assert connection.execute("SELECT status FROM sources").fetchone()[0] == "pending"
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_interactive_acceptance_routes_to_doc_account_with_controlled_extraction_fixture(
    approval_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controlled provider output proves local routing/task effects, not live quality."""
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    service = _docs_provider(monkeypatch, account="beta-corp")
    acme_before = snapshot_workflow(approval_workspace / "accounts/acme-corp")
    prompts: list[str] = []

    def controlled_provider(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            assert "Raw transcript:" in prompt and _RAW in prompt
            return _RAW
        assert len(prompts) == 2 and "Meeting text:" in prompt
        return json.dumps(
            {
                "participants": ["Seller Example", "Alex (alex@beta-corp.example.com)"],
                "action_items": ["Seller Example: Send proposal to procurement"],
                "key_decisions": ["Review platform requirements"],
                "key_topics": ["platform"],
                "confidence": "high",
            }
        )

    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", controlled_provider)

    def accept(prompt: str) -> str:
        assert prompt == "Process? [y/n/q] "
        assert isinstance(sys.stdout, StringIO)
        assert f"[{_DATE}] {_TITLE} ({_SOURCE_ID})" in sys.stdout.getvalue()
        assert prompts == []
        service.documents.assert_not_called()
        return "y"

    monkeypatch.setattr("builtins.input", accept)
    preview = invoke_workflow(["ingest", "run", "--help"])
    assert preview.exit_code == 0
    assert "--account" not in preview.stdout
    result = invoke_workflow(["ingest", "run", "--pipeline", "transcript-ingest", "--interactive", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "1 processed (0 degraded), 0 skipped, 0 error(s)" in result.stdout
    assert len(prompts) == 2
    service.documents.return_value.get.assert_called_once_with(documentId=_SOURCE_ID, includeTabsContent=True)
    note = _stored_note(approval_workspace)
    assert note.parent == approval_workspace / "accounts/beta-corp/meetings"
    content = note.read_text(encoding="utf-8")
    frontmatter = parse_meeting_frontmatter(content)
    assert frontmatter["account"] == "beta-corp"
    assert frontmatter["confidence"] == "high"
    assert frontmatter["action_items"] == ["Seller Example: Send proposal to procurement"]
    assert _RAW.encode("utf-8") in note.read_bytes()
    tasks = (approval_workspace / "TASKS.md").read_bytes()
    assert b"Send proposal to procurement" in tasks
    assert b"fieldkit-task:v1:" in tasks
    beta_pursuit = approval_workspace / "accounts/beta-corp/pursuits/platform.md"
    assert f"../meetings/{note.name}" in beta_pursuit.read_text(encoding="utf-8")
    assert snapshot_workflow(approval_workspace / "accounts/acme-corp") == acme_before
