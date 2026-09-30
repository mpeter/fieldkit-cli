"""Terminal provider authentication failures preserve ingest work for recovery."""

from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from fieldkit.__main__ import main
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.ingest.db import init_db

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("command", ["run", "reprocess"])
def test_terminal_google_401_preserves_work(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "pipeline.db"
    note = tmp_path / "accounts/acme/meetings/note.md"
    original = "---\nsource_id: source-1\npipeline: transcript-ingest\npipeline_version: 0.1.0\n---\nOriginal\n"
    service = MagicMock()
    execute = service.documents.return_value.get.return_value.execute
    execute.side_effect = HttpError(
        resp=httplib2.Response({"status": "401"}),
        content=b"provider-payload-sentinel",
        uri="https://example.com/provider-uri-sentinel",
    )
    with closing(init_db(database, pipelines=PIPELINES)) as connection:
        connection.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
            ("source-1", "transcript-ingest", "source", "pending" if command == "run" else "processed"),
        )
        if command == "reprocess":
            note.parent.mkdir(parents=True)
            note.write_text(original, encoding="utf-8")
            note.chmod(0o600)
            connection.execute(
                "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("artifact-1", "source-1", "transcript-ingest", "vault_note", str(note), "0.1.0"),
            )
        connection.commit()
        with (
            patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
            patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path / "runtime"),
            patch("fieldkit.ingest.db.get_db_path", return_value=database),
            patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
            patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
            patch("fieldkit.ingest.docs.get_docs_service", return_value=service),
        ):
            argv = ["ingest", command, "--pipeline", "transcript-ingest"]
            if command == "reprocess":
                argv.extend(["--from-version", "0.1.0"])
            result = main(argv)
        assert result == 2
        execute.assert_called_once()
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
        if command == "run":
            assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
            assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
            assert not note.exists()
        else:
            assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"
            assert note.read_text(encoding="utf-8") == original
            assert note.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "fieldkit auth google" in output.err
    assert "sentinel" not in output.out + output.err + caplog.text
    assert "Traceback" not in output.err
