"""Tests for the `fieldkit pursuit repair-dates` CLI command.

Business logic lives in ``fieldkit.watch.repair``; these tests cover only the
Click wiring in ``fieldkit.commands.pursuit.repair_dates_cmd``. Domain-level
tests for ``_repair_one`` live in ``tests/test_watch_repair.py`` and
``tests/test_watch_repair_branches.py``.

Tasks 2.6-2.7: cli config-error, cli empty-pursuits

Patches target ``fieldkit.watch.repair.*`` — the domain module binding site.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.repair_dates_cmd import cli
from fieldkit.config import ConfigError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Task 2.6 — cli exits non-zero when get_fieldkit_home raises ConfigError
# ---------------------------------------------------------------------------


def test_cli_exits_nonzero_on_config_error() -> None:
    """The top-level dispatcher maps repair ConfigError to the data-error code.

    We mock get_fieldkit_home at the domain module's import namespace so the
    patch is effective regardless of where ConfigError originates, then invoke
    the real entry point that owns exception-to-exit-code mapping.
    """
    from fieldkit.__main__ import main

    with patch(
        "fieldkit.watch.repair.get_fieldkit_home",
        side_effect=ConfigError("no config file found"),
    ):
        # _accounts_dir is @cache-decorated; clear it so our mock is actually called
        from fieldkit.watch.repair import _accounts_dir

        _accounts_dir.cache_clear()

        exit_code = main(["pursuit", "repair-dates", "--dry-run"])

    assert exit_code == 3


# ---------------------------------------------------------------------------
# Task 2.7 — cli exits zero when data dir exists but contains no pursuit files
# ---------------------------------------------------------------------------


def test_cli_exits_zero_no_pursuits(tmp_path: Path) -> None:
    """cli exits 0 when the accounts directory exists but holds no pursuit files.

    We point get_fieldkit_home at a real tmp_path directory that has the expected
    ``accounts/`` sub-structure but no *.md files under any pursuits/ folder.
    The command should complete successfully (exit 0) with a summary of 0 repairs.
    """
    # Build a minimal accounts tree with no pursuit files
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    # An account dir without a pursuits/ sub-dir — nothing to scan
    (accounts_dir / "acme-corp").mkdir()

    runner = CliRunner()

    with patch("fieldkit.watch.repair.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.watch.repair import _accounts_dir

        _accounts_dir.cache_clear()

        result = runner.invoke(cli, ["--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0, (
        f"Expected exit 0 with empty pursuits dir, got {result.exit_code}.\nOutput:\n{result.output}"
    )
    # Sanity-check the summary line is present
    assert "0 pursuit(s)" in result.output


def test_cli_json_reports_empty_dry_run(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    with patch("fieldkit.watch.repair.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.watch.repair import _accounts_dir

        _accounts_dir.cache_clear()
        result = CliRunner().invoke(cli, ["--dry-run", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {"account": None, "dry_run": True, "repair_count": 0, "repairs": [], "skipped_count": 0}
