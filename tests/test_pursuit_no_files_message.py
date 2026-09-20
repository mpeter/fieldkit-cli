"""Tests for the standardized "no files found" message (implementation change)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.audit import no_files_message
from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli
from fieldkit.commands.pursuit.forecast import cli as forecast_cli
from fieldkit.commands.pursuit.pipeline_health import cli as health_cli
from fieldkit.commands.pursuit.projects_health import cli as projects_cli


@pytest.mark.unit
def test_no_files_message_with_account() -> None:
    assert no_files_message("pursuit", "acme-internal") == "No pursuit files found for account: acme-internal"


@pytest.mark.unit
def test_no_files_message_without_account() -> None:
    assert no_files_message("pursuit", None) == "No pursuit files found."


@pytest.mark.unit
def test_no_files_message_project_kind() -> None:
    assert no_files_message("project", "acme-corp") == "No project files found for account: acme-corp"


@pytest.mark.unit
def test_no_files_message_never_contains_path_separator() -> None:
    # implementation change: regression guard — the message must never leak a filesystem path.
    msg = no_files_message("pursuit", "acme-internal")
    assert "/" not in msg
    assert "\\" not in msg


# ---------------------------------------------------------------------------
# CLI-level "Accounts directory not found" regression coverage.
#
# Follow-up to implementation change review: the four commands' *own* helper (no_files_message)
# was already covered above, but nothing asserted on the output of the sibling
# "Accounts directory not found" branch that each command also has — which is
# exactly the same path-leak defect class this work order was filed for. This
# left forecast.py's identical leak (`{accounts_dir}` in the message) and the
# regression-prone "run X to fix it" remedy text (previously pointed at
# `fieldkit pursuit create`, which crashes with an unhandled FileNotFoundError
# when accounts/ doesn't exist yet — `fieldkit init` is the command that
# actually creates it) uncovered by any test.
# ---------------------------------------------------------------------------

_ACCOUNTS_DIR_MISSING_CASES = [
    ("audit", audit_cli, "fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home"),
    ("health", health_cli, "fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home"),
    ("forecast", forecast_cli, "fieldkit.commands.pursuit.forecast.get_fieldkit_home"),
    ("projects", projects_cli, "fieldkit.commands.pursuit.projects_health.get_fieldkit_home"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,cli,patch_target", _ACCOUNTS_DIR_MISSING_CASES, ids=[c[0] for c in _ACCOUNTS_DIR_MISSING_CASES]
)
def test_accounts_dir_missing_message_never_leaks_path(
    name: str, cli: object, patch_target: str, tmp_path: Path
) -> None:
    """No command's 'Accounts directory not found' message may echo an absolute path."""
    with patch(patch_target, return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [], catch_exceptions=False)  # type: ignore[arg-type]

    assert result.exit_code == 3, f"{name}: expected exit 3, got {result.exit_code}:\n{result.output}"
    assert str(tmp_path) not in result.output, f"{name}: leaked the tmp_path fixture's absolute path"
    assert "Accounts directory not found" in result.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,cli,patch_target", _ACCOUNTS_DIR_MISSING_CASES, ids=[c[0] for c in _ACCOUNTS_DIR_MISSING_CASES]
)
def test_accounts_dir_missing_message_points_to_working_remedy(
    name: str, cli: object, patch_target: str, tmp_path: Path
) -> None:
    """The suggested remedy must be a command that actually creates accounts/ —
    'fieldkit pursuit create' requires accounts/<account>/ to already exist and
    crashes otherwise; 'fieldkit init' is the command that scaffolds it."""
    with patch(patch_target, return_value=tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [], catch_exceptions=False)  # type: ignore[arg-type]

    assert "fieldkit init" in result.output, f"{name}: remedy text should point to 'fieldkit init':\n{result.output}"
    assert "pursuit create" not in result.output, (
        f"{name}: must not recommend 'pursuit create' here — it requires accounts/ to already "
        f"exist and raises an unhandled FileNotFoundError otherwise:\n{result.output}"
    )
