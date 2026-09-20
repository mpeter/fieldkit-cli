"""Behavioral coverage for the ``fieldkit contact report`` CLI adapter.

Targets ``cli`` in ``src/fieldkit/commands/contact/report_cmd.py``. The domain
function ``build_report`` is mocked throughout — this file tests the Click
adapter's branching (no-report guard, ``--json`` vs plain text, ``--account``
threading), not the report-building logic itself.
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.contact.report_cmd import cli

pytestmark = pytest.mark.unit

NO_REPORT_MESSAGE = "No enriched contacts found. Run 'fieldkit contact enrich' to build the enrichment index first."


def _make_runner() -> CliRunner:
    return CliRunner()


def test_no_report_prints_guidance_and_exits_cleanly() -> None:
    """build_report() returning None prints the guidance message and exits 0."""
    runner = _make_runner()
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value=None):
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    assert result.output == NO_REPORT_MESSAGE + "\n"


def test_report_without_json_flag_echoes_raw_report() -> None:
    """A non-empty report, no --json, is echoed verbatim (not JSON-wrapped)."""
    runner = _make_runner()
    report_text = "# Contact Enrichment Report\n\n- acme.com: 12/15 enriched"
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value=report_text):
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    assert result.output == report_text + "\n"


def test_report_with_json_flag_wraps_in_report_key() -> None:
    """A non-empty report with --json is emitted as {"report": <string>}."""
    import json

    runner = _make_runner()
    report_text = "# Contact Enrichment Report\n\n- acme-corp.com: 3/5 enriched"
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value=report_text):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == {"report": report_text}


def test_account_option_is_threaded_through_to_build_report() -> None:
    """--account <slug> is forwarded to build_report(account=<slug>)."""
    runner = _make_runner()
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value="report") as mock_build:
        result = runner.invoke(cli, ["--account", "acme"])

    assert result.exit_code == 0
    mock_build.assert_called_once_with(account="acme")


def test_omitted_account_option_calls_build_report_with_none() -> None:
    """Omitting --account calls build_report(account=None)."""
    runner = _make_runner()
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value="report") as mock_build:
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    mock_build.assert_called_once_with(account=None)


def test_none_report_guard_precedes_json_branch() -> None:
    """When build_report() returns None AND --json is passed, the human
    guidance message still wins — the None-report guard must run before the
    --json branch is ever reached, not the reverse.
    """
    runner = _make_runner()
    with patch("fieldkit.commands.contact.report_cmd.build_report", return_value=None):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    assert result.output == NO_REPORT_MESSAGE + "\n"
    assert "report" not in result.output
