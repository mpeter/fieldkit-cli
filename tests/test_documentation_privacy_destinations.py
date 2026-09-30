"""Prove fieldkit's published destinations at mocked client boundaries."""

import json
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.backstory import auth as backstory
from fieldkit.driver import spend
from fieldkit.google_oauth import build_google_service
from fieldkit.gtask.client import list_tasks
from fieldkit.issue import GHIssueStore
from fieldkit.llm import core as llm_core
from fieldkit.sf.client import SFDirectClient
from fieldkit.shadowbot import client as shadowbot
from fieldkit.watch import mcp
from scripts.markdown_tables import MarkdownTable, markdown_tables, parse_markdown_tables

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parent.parent
_MCP_DESTINATION = "The configured MCP endpoint and its downstream services; Backstory authorization registers the People.ai MCP endpoint through MCPJungle"
_SNAPSHOT_SCOPE = "The verified SQLite snapshot reader does not open the original database through SQLite."
_SPEND_EXCEPTION = (
    "These are guarantees of that reader, not of every command labeled read-only. "
    "Optional automation spend accounting opens its external session database directly "
    "in SQLite read-only mode; it does not provide the same verified-snapshot contract."
)


def _assert_destinations(table: MarkdownTable) -> None:
    assert table.header == ("Capability", "Typical data destination")
    assert table.rows == (
        ("Salesforce", "The Salesforce organization you configure"),
        ("Google", "Gmail, Drive, Docs, or other enabled Google APIs"),
        ("LLM", "The configured supported model provider"),
        ("MCP-backed tools", _MCP_DESTINATION),
        (
            "GitHub-backed issue commands",
            "The GitHub repository configured for fieldkit issues; issue titles, bodies, labels, and status changes",
        ),
        (
            "Organization-provided services",
            "The service endpoint configured by your operator; for example, a ShadowBot prompt, thread identifier, and returned response",
        ),
    ), "Unapproved destination policy"


def _assert_privacy_document(document: str) -> None:
    tables = parse_markdown_tables(document)
    assert len(tables) == 1
    _assert_destinations(tables[0])
    _assert_outbound_paragraph(document)
    _assert_sqlite_reader_scope(document)


def test_privacy_destination_table_and_authentication_scope() -> None:
    _assert_privacy_document((_ROOT / "docs/privacy.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutation", ["universal-reader-claim", "missing-spend-exception", "negated-spend-exception"])
def test_privacy_document_rejects_false_sqlite_scope(mutation: str) -> None:
    document = (_ROOT / "docs/privacy.md").read_text(encoding="utf-8")
    if mutation == "universal-reader-claim":
        reader = next(
            paragraph for paragraph in document.split("\n\n") if _SNAPSHOT_SCOPE in " ".join(paragraph.split())
        )
        replacement = " ".join(reader.split()).replace(
            _SNAPSHOT_SCOPE,
            "Read-only commands do not open the original database through SQLite.",
        )
        mutated = document.replace(reader, replacement)
    else:
        paragraphs = document.split("\n\n")
        exception = next(paragraph for paragraph in paragraphs if "Optional automation spend accounting" in paragraph)
        replacement = "" if mutation == "missing-spend-exception" else f"It is false that {exception}"
        mutated = document.replace(exception, replacement)
    assert mutated != document
    with pytest.raises(AssertionError, match="SQLite privacy scope"):
        _assert_privacy_document(mutated)


def _assert_sqlite_reader_scope(document: str) -> None:
    paragraphs = tuple(" ".join(paragraph.split()) for paragraph in document.split("\n\n"))
    readers = tuple(paragraph for paragraph in paragraphs if "verified, quiescent database" in paragraph)
    assert len(readers) == 1 and readers[0].startswith(f"{_SNAPSHOT_SCOPE} "), "Unapproved SQLite privacy scope"
    assert not any("Read-only commands do not open" in paragraph for paragraph in paragraphs), (
        "Unapproved SQLite privacy scope"
    )
    exceptions = tuple(paragraph for paragraph in paragraphs if "Optional automation spend accounting" in paragraph)
    assert exceptions == (_SPEND_EXCEPTION,), "Unapproved SQLite privacy scope"


def test_optional_automation_spend_opens_original_session_database_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "automation-session.db"
    real_connect = sqlite3.connect
    with real_connect(database) as connection:
        connection.execute("CREATE TABLE project(id TEXT, worktree TEXT)")
        connection.execute("CREATE TABLE session(project_id TEXT, time_created INTEGER, cost REAL, model TEXT)")
        connection.execute("INSERT INTO project VALUES ('example-project', ?)", (str(tmp_path),))
        connection.execute(
            "INSERT INTO session VALUES ('example-project', ?, 0.75, ?)",
            (
                int(datetime.now(UTC).timestamp() * 1000),
                json.dumps({"providerID": spend._DEVELOPER_PROVIDER, "id": spend._DEVELOPER_MODEL}),
            ),
        )
    connection.close()
    before = database.read_bytes()
    opened: list[tuple[str, bool]] = []
    connections: list[sqlite3.Connection] = []

    def direct_read(path: str, *, uri: bool = False) -> sqlite3.Connection:
        opened.append((path, uri))
        connection = real_connect(path, uri=uri)
        connections.append(connection)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO project VALUES ('unapproved', 'unapproved')")
        return connection

    monkeypatch.setenv("FIELDKIT_OPENCODE_DB", str(database))
    monkeypatch.setattr("fieldkit.driver.spend.sqlite3.connect", direct_read)
    result = spend.get_daily_developer_spend_total()

    assert result == 0.75
    assert opened == [(f"file:{database}?mode=ro", True)]
    assert database.read_bytes() == before
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_privacy_outbound_paragraph_rejects_negated_authentication_claim() -> None:
    document = (_ROOT / "docs/privacy.md").read_text(encoding="utf-8")
    mutated = document.replace(
        "authentication may also contact provider login and token endpoints",
        "it is false that authentication may also contact provider login and token endpoints",
    )
    assert mutated != document
    with pytest.raises(AssertionError, match="outbound policy"):
        _assert_outbound_paragraph(mutated)


def _assert_outbound_paragraph(document: str) -> None:
    section = document.split("## What can leave the machine\n", maxsplit=1)[1]
    paragraph = section.split("| Capability |", maxsplit=1)[0]
    assert " ".join(paragraph.split()) == (
        "Commands using external APIs, provider clients, or authentication helpers can send requests "
        "beyond the workstation. The table summarizes common destinations; authentication may also "
        "contact provider login and token endpoints."
    ), "Unapproved outbound policy"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("The Salesforce organization you configure", "No data reaches the Salesforce organization you configure"),
        ("Gmail, Drive, Docs, or other enabled Google APIs", "Only local files, never Google APIs"),
        ("The configured supported model provider", "An arbitrary model provider"),
        (_MCP_DESTINATION, "Only the endpoint you configure; no authentication destination exists"),
        ("The GitHub repository configured for fieldkit issues", "Any public GitHub repository"),
        ("The service endpoint configured by your operator", "No service endpoint configured by your operator"),
    ],
)
def test_privacy_destination_table_rejects_false_claims(before: str, after: str) -> None:
    source = markdown_tables(_ROOT / "docs/privacy.md")[0].source_text
    assert before in source
    table = parse_markdown_tables(source.replace(before, after))[0]
    with pytest.raises(AssertionError, match="destination policy"):
        _assert_destinations(table)


def test_salesforce_destination_is_the_supplied_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    http = MagicMock()
    response = http.request.return_value
    response.status_code = 204
    monkeypatch.setattr("fieldkit.sf.client.httpx.Client", lambda **_kwargs: http)
    with SFDirectClient(session_id="fictional-session", base_url="https://acme.my.salesforce.com") as client:
        result = client.update_opportunity_fields("006A100000TestOppId", {"StageName": "Closed Won"})
    assert result is None
    assert http.request.call_args.args[:2] == (
        "PATCH",
        "https://acme.my.salesforce.com/services/data/v59.0/sobjects/Opportunity/006A100000TestOppId",
    )
    assert http.request.call_args.kwargs["json"] == {"StageName": "Closed Won"}


@pytest.mark.parametrize("api,version", [("gmail", "v1"), ("drive", "v3"), ("docs", "v1")])
def test_google_destination_uses_requested_provider_api(
    monkeypatch: pytest.MonkeyPatch, api: str, version: str
) -> None:
    build = MagicMock()
    monkeypatch.setattr("googleapiclient.discovery.build", build)
    result = build_google_service(api, version, MagicMock())
    assert result is build.return_value
    assert build.call_args.args == (api, version)


def test_google_tasks_route_uses_gws_tasks_api() -> None:
    runner = MagicMock(
        side_effect=[json.dumps({"items": [{"id": "list-id", "title": "fieldkit"}]}), json.dumps({"items": []})]
    )
    result = list_tasks(gws_runner=runner)
    assert result == []
    assert runner.call_args_list[0].args[0][:3] == ["tasks", "tasklists", "list"]
    assert runner.call_args_list[1].args[0][:3] == ["tasks", "tasks", "list"]
    assert json.loads(runner.call_args_list[1].args[0][4])["tasklist"] == "list-id"


def test_backstory_authentication_registers_fixed_provider_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backstory, "get_mcp_gateway_url", lambda **_kwargs: "https://gateway.example.com")
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    result = backstory.authenticate(runner=runner, stdin_is_tty=lambda: True)
    assert result is None
    argv = runner.call_args.args[0]
    assert argv[argv.index("--registry") + 1] == "https://gateway.example.com"
    assert argv[argv.index("--url") + 1] == "https://mcp.people.ai/mcp"


def test_github_issue_destination_and_selected_content(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout='{"number": 43}', stderr=""))
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", runner)
    result = GHIssueStore("acme/example").create(
        issue_type="bug", title="Example", body="Fictional description", severity="high", module="sf", source="test"
    )
    assert result.gh_number == 43
    argv = runner.call_args.args[0]
    assert argv[1:3] == ["api", "repos/acme/example/issues"]
    assert "title=Example" in argv
    assert any(argument.startswith("body=") and "Fictional description" in argument for argument in argv)


def test_llm_destination_receives_selected_model_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    completion = MagicMock()
    completion.return_value.choices = [MagicMock(message=MagicMock(content="Fictional response"))]
    monkeypatch.setattr(llm_core, "llm_disabled", lambda: False)
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("fieldkit.llm.log._ensure_initialized", lambda: None)
    result = llm_core.synthesize("Fictional prompt", model="vertex_ai/claude-sonnet-4-6")
    assert result == "Fictional response"
    assert completion.call_args.kwargs["model"] == "vertex_ai/claude-sonnet-4-6"
    assert completion.call_args.kwargs["messages"] == [{"role": "user", "content": "Fictional prompt"}]


def test_mcp_destination_receives_selected_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = MagicMock(
        return_value=({}, b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{}"}]}}')
    )
    monkeypatch.setattr(mcp, "_post_with_retry", transport)
    session = mcp.MCPSession("https://gateway.example.com/mcp")
    try:
        session._session_id = "fictional-session"
        result = session.call_tool("example_tool", {"account": "acme"})
    finally:
        session.close()
    assert result == {}
    assert transport.call_args.args[2] == "https://gateway.example.com/mcp"
    assert json.loads(transport.call_args.args[3])["params"] == {
        "name": "example_tool",
        "arguments": {"account": "acme"},
    }


def test_shadowbot_destination_receives_prompt_and_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadowbot, "get_shadowbot_api_base", lambda: "https://assistant.example.com")
    monkeypatch.setattr("fieldkit.config.get_shadowbot_assistant_id", lambda: "example-assistant")
    response = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(shadowbot, "_call_until_deadline", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(shadowbot, "_lines_until_deadline", lambda *_args: iter([]))
    parsed = shadowbot.ShadowbotResponse(content="Fictional response")
    monkeypatch.setattr(shadowbot, "_parse_sse_stream", lambda _lines: parsed)
    http = MagicMock()
    result = shadowbot.ShadowbotClient("fictional-token")._stream_query(
        client=http,
        thread_id="example-thread",
        prompt="Fictional prompt",
        headers={},
        deadline=time.monotonic() + 10,
        total_timeout=10,
    )
    assert result is parsed
    assert http.build_request.call_args.args == (
        "POST",
        "https://assistant.example.com/threads/example-thread/runs/stream",
    )
    payload = http.build_request.call_args.kwargs["json"]
    assert payload["input"]["messages"] == [{"role": "human", "content": "Fictional prompt"}]
    assert payload["assistant_id"] == "example-assistant"
