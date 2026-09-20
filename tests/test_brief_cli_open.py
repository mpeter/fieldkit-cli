import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.brief.cli import cmd_open

pytestmark = pytest.mark.unit


def test_brief_open_json_reports_selected_file_and_opens_it(tmp_path: Path) -> None:
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    brief = briefs_dir / "morning-brief-2026-09-10.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    fresh_time = time.time() - 3600
    os.utime(brief, (fresh_time, fresh_time))

    with (
        patch("fieldkit.commands.brief.cli.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(cmd_open, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"] == str(brief)
    assert payload["opened"] is True
    assert payload["stale"] is False
    mock_open.assert_called_once_with(brief.as_uri())
