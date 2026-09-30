"""Executable scenarios for safe commands shown in the public brief guide."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from scripts.check_documentation_contract import fenced_blocks
from tests.documentation_workflow_support import (
    DOCUMENTED_DATE,
    invoke_workflow,
    prepare_documented_gmail_cache,
    snapshot_workflow,
    write_documented_pursuit_data,
)

pytestmark = pytest.mark.integration

_BRIEF_PAGES = (
    Path("src/fieldkit/skills/brief/SKILL.md"),
    Path("src/fieldkit/skills/brief/ops/update.md"),
    Path("src/fieldkit/skills/brief/ops/week-end.md"),
    Path("src/fieldkit/skills/brief/ops/week-start.md"),
    Path("src/fieldkit/skills/brief/references/comprehensive-scan.md"),
)
_MORNING_BRIEF_PAGE = Path("docs/guides/morning-brief.md")
_SAFE_SCENARIO_COMMANDS = {
    "morning-preview": "fieldkit brief generate --dry-run",
    "morning-account-preview": "fieldkit brief generate --dry-run --account acme-corp",
    "morning-date-preview": "fieldkit brief generate --date 2026-09-10 --dry-run",
    "morning-verbose-preview": "fieldkit brief generate --verbose --dry-run",
    "morning-help": "fieldkit brief generate --help",
    "brief-preview": "fieldkit brief generate --pipeline-only --no-llm --dry-run",
    "sync-preview": "fieldkit sync --sf --dry-run",
    "pipeline-report": "fieldkit pipeline --no-llm",
    "project-health": "fieldkit pursuit projects --json",
    "pursuit-audit": "fieldkit pursuit audit --json",
    "gmail-account": "fieldkit gmail query account acme-corp --since YYYY-MM-DD --limit 10 --json",
    "gmail-decay": "fieldkit gmail decay --account acme-corp --limit 10 --json",
    "gmail-blindspots": "fieldkit gmail query blindspots acme-corp --since YYYY-MM-DD --limit 10 --json",
}
_SKILL_SAFE_SCENARIOS = frozenset(
    {
        "morning-preview",
        "brief-preview",
        "sync-preview",
        "pipeline-report",
        "project-health",
        "pursuit-audit",
        "gmail-account",
        "gmail-decay",
        "gmail-blindspots",
    }
)
_MANUAL_DOCUMENTED_COMMANDS: set[str] = set()


@pytest.mark.parametrize("scenario", ("present", "missing", "empty", "symlink"))
def test_documented_saved_brief_selection_is_read_only(
    documented_workspace: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    page = Path("docs/guides/morning-brief.md")
    blocks = fenced_blocks(page)
    assert blocks[-1].body.strip() == "fieldkit brief open --no-open --json"
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][page.as_posix()]["fenced_blocks"]
    assert len(records) == len(blocks)
    assert records[-1]["verification_id"] == "automated.brief-workflow-scenarios"
    reports = documented_workspace / "briefs"
    reports.mkdir()
    report = reports / "morning-brief-2026-09-27.md"
    if scenario == "symlink":
        outside = documented_workspace.parent / "private.md"
        outside.write_text("private-marker", encoding="utf-8")
        report.symlink_to(outside)
    elif scenario != "missing":
        report.write_text("# Fictional brief\n" if scenario == "present" else "", encoding="utf-8")
    before = snapshot_workflow(documented_workspace.parent)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("metadata selection launched a viewer")

    monkeypatch.setattr("webbrowser.open", forbidden)
    result = invoke_workflow(["brief", "open", "--no-open", "--json"])

    assert result.exit_code == (0 if scenario == "present" else 3), result.output
    if scenario == "present":
        payload = json.loads(result.stdout)
        assert payload["path"] == str(report)
        assert payload["opened"] is False
    assert "private-marker" not in result.output
    assert snapshot_workflow(documented_workspace.parent) == before


def _scenario_argv(identifier: str) -> list[str]:
    command = _SAFE_SCENARIO_COMMANDS[identifier].replace("YYYY-MM-DD", DOCUMENTED_DATE)
    argv = shlex.split(command)
    assert argv[0] == "fieldkit"
    return argv[1:]


def _fenced_fieldkit_commands(path: Path) -> set[str]:
    blocks = (block.body for block in fenced_blocks(path))
    return {line.strip() for block in blocks for line in block.splitlines() if line.strip().startswith("fieldkit ")}


def test_brief_pages_bind_exactly_to_reviewed_safe_and_manual_commands() -> None:
    documented = set().union(*(_fenced_fieldkit_commands(path) for path in _BRIEF_PAGES))
    safe_commands = {_SAFE_SCENARIO_COMMANDS[identifier] for identifier in _SKILL_SAFE_SCENARIOS}

    assert documented == safe_commands | _MANUAL_DOCUMENTED_COMMANDS
    assert _MANUAL_DOCUMENTED_COMMANDS.isdisjoint(safe_commands)


def test_morning_brief_fences_have_one_behavior_appropriate_owner() -> None:
    """Separate safe previews from diagnostics, writes, and saved-report selection."""
    blocks = fenced_blocks(_MORNING_BRIEF_PAGE)
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][_MORNING_BRIEF_PAGE.as_posix()]["fenced_blocks"]
    assert len(records) == len(blocks)
    ownership = {block.body.strip(): record["verification_id"] for block, record in zip(blocks, records, strict=True)}
    assert ownership["fieldkit doctor"] == "automated.installed-base-artifact"
    assert ownership["fieldkit brief generate"] == "manual.credentialed-integration"
    assert ownership["fieldkit brief open --no-open --json"] == "automated.brief-workflow-scenarios"
    for command in (
        "fieldkit brief generate --dry-run",
        "fieldkit brief generate --dry-run --account acme-corp",
        "fieldkit brief generate --pipeline-only --no-llm --dry-run",
        "fieldkit brief generate --date 2026-09-10 --dry-run\nfieldkit brief generate --verbose --dry-run",
        "fieldkit brief generate --help",
    ):
        assert ownership[command] == "automated.brief-workflow-scenarios"


@pytest.mark.parametrize(
    "document",
    [
        "~~~console\nfieldkit unreviewed --unsafe\n~~~\n",
        "````text\nfieldkit unreviewed --unsafe\n````\n",
        "   ```console\n   fieldkit unreviewed --unsafe\n   ```\n",
    ],
    ids=("tilde", "long-backtick", "indented"),
)
def test_commonmark_fences_cannot_hide_an_unreviewed_fieldkit_command(tmp_path: Path, document: str) -> None:
    page = tmp_path / "brief-page.md"
    page.write_text(document, encoding="utf-8")

    commands = _fenced_fieldkit_commands(page)

    assert commands == {"fieldkit unreviewed --unsafe"}
    assert commands - set(_SAFE_SCENARIO_COMMANDS.values()) - _MANUAL_DOCUMENTED_COMMANDS


def test_documented_offline_generation_scenarios_execute_with_exact_argv(
    documented_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_documented_pursuit_data(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline documentation scenario reached an external effect")

    monkeypatch.setattr("fieldkit.brief.pipeline_only.synthesize", forbidden)
    monkeypatch.setattr("fieldkit.pipeline.main.synthesize", forbidden)
    monkeypatch.setattr("fieldkit.commands.datasync.cli._execute_step", forbidden)

    brief = invoke_workflow(_scenario_argv("brief-preview"))
    assert brief.exit_code == 0, brief.output
    assert "Morning Brief" in brief.output
    assert snapshot_workflow(documented_workspace.parent) == before

    sync = invoke_workflow(_scenario_argv("sync-preview"))
    assert sync.exit_code == 0, sync.output
    assert "dry-run" in sync.output
    assert snapshot_workflow(documented_workspace.parent) == before

    pipeline = invoke_workflow(_scenario_argv("pipeline-report"))
    assert pipeline.exit_code == 0, pipeline.output
    reports = list((documented_workspace / "briefs").glob("pipeline-review-*.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "## Pursuit Health Table" in report
    assert "## Narrative Summary" not in report
    after = snapshot_workflow(documented_workspace.parent)
    assert all(after[path] == entry for path, entry in before.items() if path != "workspace")
    assert after["workspace"][0] == before["workspace"][0]
    assert set(after) - set(before) == {"workspace/briefs", f"workspace/briefs/{reports[0].name}"}


@pytest.mark.parametrize(
    "scenario",
    (
        "morning-preview",
        "morning-account-preview",
        "morning-date-preview",
        "morning-verbose-preview",
        "morning-help",
    ),
)
def test_documented_morning_brief_previews_are_offline_and_read_only(
    documented_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Run every no-credential guide preview with its exact published argv."""
    write_documented_pursuit_data(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("morning brief preview reached an external provider")

    monkeypatch.setattr("fieldkit.brief.pipeline_only.synthesize", forbidden)
    result = invoke_workflow(_scenario_argv(scenario))

    assert result.exit_code == 0, result.output
    if scenario == "morning-help":
        assert "--dry-run" in result.stdout
    else:
        assert "Morning Brief" in result.stdout
        assert "not run" in result.stdout
    assert snapshot_workflow(documented_workspace.parent) == before


def test_documented_pursuit_json_scenarios_are_read_only(documented_workspace: Path) -> None:
    write_documented_pursuit_data(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    projects = invoke_workflow(_scenario_argv("project-health"))
    assert projects.exit_code == 0, projects.output
    project_rows = json.loads(projects.stdout)
    assert project_rows[0]["name"] == "acme-corp/implementation"
    assert snapshot_workflow(documented_workspace.parent) == before

    audit = invoke_workflow(_scenario_argv("pursuit-audit"))
    assert audit.exit_code == 0, audit.output
    audit_rows = json.loads(audit.stdout)
    assert audit_rows[0]["relative_path"] == "acme-corp/pursuits/platform.md"
    assert "path" not in audit_rows[0]
    assert snapshot_workflow(documented_workspace.parent) == before


@pytest.mark.parametrize(
    "scenario",
    ["gmail-account", "gmail-decay", "gmail-blindspots"],
    ids=("account", "decay", "blindspots"),
)
def test_documented_cached_gmail_scenarios_are_bounded_and_read_only(
    documented_workspace: Path,
    scenario: str,
) -> None:
    prepare_documented_gmail_cache(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)
    argv = _scenario_argv(scenario)

    result = invoke_workflow(argv)

    assert result.exit_code == 0, (argv, result.output)
    payload = json.loads(result.stdout)
    if scenario == "gmail-account":
        assert payload["count"] == 1
        assert payload["filters"]["limit"] == 10
    elif scenario == "gmail-decay":
        assert payload["account"] == "acme-corp"
        assert isinstance(payload["contacts"], list)
    else:
        assert payload["count"] == 1
        assert payload["items"][0]["email"] == "alex@acme-corp.example.com"
    assert snapshot_workflow(documented_workspace.parent) == before


@pytest.mark.parametrize(
    ("argv", "expected_exit"),
    [
        (_scenario_argv("project-health"), 3),
        (_scenario_argv("pursuit-audit"), 3),
        (_scenario_argv("gmail-account"), 1),
    ],
)
def test_documented_read_scenarios_fail_closed_when_data_is_missing(
    documented_workspace: Path,
    argv: list[str],
    expected_exit: int,
) -> None:
    before = snapshot_workflow(documented_workspace.parent)
    result = invoke_workflow(argv)
    assert result.exit_code == expected_exit, result.output
    assert snapshot_workflow(documented_workspace.parent) == before
