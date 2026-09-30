"""Executable scenarios for safe commands shown in the public watcher guide."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from scripts.check_documentation_contract import fenced_blocks
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow, write_documented_pursuit_data

pytestmark = pytest.mark.integration

_WATCHER_PAGE = Path("docs/guides/watchers.md")
_SAFE_COMMANDS = (
    "fieldkit watch run pursuit-stalls --dry-run",
    "fieldkit watch run --all --dry-run",
    "fieldkit watch run --all --install-cron --cron-time '0 6 * * *' --dry-run",
)


def _argv(command: str) -> list[str]:
    argv = shlex.split(command)
    assert argv[0] == "fieldkit"
    return argv[1:]


def test_watcher_fences_have_one_behavior_appropriate_owner() -> None:
    """Keep previews automated while live providers and crontab writes stay manual."""
    blocks = fenced_blocks(_WATCHER_PAGE)
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][_WATCHER_PAGE.as_posix()]["fenced_blocks"]
    assert len(records) == len(blocks)
    ownership = {block.body.strip(): record["verification_id"] for block, record in zip(blocks, records, strict=True)}

    for command in _SAFE_COMMANDS:
        assert ownership[command] == "automated.watcher-workflow-scenarios"
    assert ownership["fieldkit watch run --all\nfieldkit watch run --all --slack"] == "manual.credentialed-integration"
    assert (
        ownership["fieldkit watch run --all --install-cron --cron-time '0 6 * * *'"]
        == "manual.credentialed-integration"
    )


@pytest.mark.parametrize("command", _SAFE_COMMANDS[:2])
def test_documented_watcher_previews_are_offline_and_read_only(
    documented_workspace: Path,
    command: str,
) -> None:
    """Execute local watcher previews without credentials, providers, or writes."""
    write_documented_pursuit_data(documented_workspace)
    pursuit = documented_workspace / "accounts" / "acme-corp" / "pursuits" / "platform.md"
    pursuit.write_text(
        pursuit.read_text(encoding="utf-8").replace("sf_close_date: 12/31/2027", "sf_close_date: 2027-12-31"),
        encoding="utf-8",
    )
    (documented_workspace / "TASKS.md").write_text("# Tasks\n\n## Active\n\n## Waiting On\n", encoding="utf-8")
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(_argv(command))

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower() or "Watcher Summary" in result.stdout
    assert snapshot_workflow(documented_workspace.parent) == before


def test_documented_cron_preview_uses_fixed_binary_without_subprocess_or_writes(
    documented_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the published cron preview without reading or replacing host crontab."""
    before = snapshot_workflow(documented_workspace.parent)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cron preview invoked a subprocess")

    monkeypatch.setattr("fieldkit.commands.watch.cli.shutil.which", lambda _name: "/opt/fieldkit/bin/fieldkit")
    monkeypatch.setattr("fieldkit.commands.watch.cli.subprocess.run", forbidden)
    result = invoke_workflow(_argv(_SAFE_COMMANDS[2]))

    assert result.exit_code == 0, result.output
    assert result.stdout == "[dry-run] would add crontab entry: 0 6 * * * /opt/fieldkit/bin/fieldkit watch run --all\n"
    assert snapshot_workflow(documented_workspace.parent) == before
