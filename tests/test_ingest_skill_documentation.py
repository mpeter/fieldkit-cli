"""Public ingest-skill safety and command-contract checks."""

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.import_cache import cli as import_cache_cli
from fieldkit.commands.gmail.sync_command import cli as gmail_sync_cli
from scripts.check_documentation_contract import fenced_blocks

pytestmark = pytest.mark.unit

SKILL_ROOT = Path("src/fieldkit/skills/ingest")


def _fenced_commands(path: Path) -> tuple[str, ...]:
    """Return command lines from shell-like fenced examples."""
    return tuple(
        line.strip()
        for block in fenced_blocks(path)
        if block.language in {"bash", "console", "sh"}
        for line in block.body.splitlines()
        if line.strip()
    )


def test_ingest_skill_fails_closed_on_global_pending_queue() -> None:
    """The default transcript workflow cannot imply account-scoped execution."""
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert text
    assert "does not accept `--account`" in text
    assert "explicit approval" in text
    assert "without registering them or initializing the registry" in text
    assert "preview remains global, not account-scoped" in text
    assert "may initialize `pipeline.db` and register sources" not in text
    assert "fieldkit ingest discover --pipeline transcript-ingest --dry-run --account <account> --json" in text
    assert "fieldkit ingest run --pipeline transcript-ingest --interactive" in text
    assert "fieldkit ingest run --pipeline transcript-ingest --interactive --json" not in text
    assert "counts remain global" in text
    assert "Account-only registration is unavailable" in text
    assert "fieldkit ingest discover --pipeline transcript-ingest --limit <N> --json" in text
    assert "subjects, and dates" not in text
    assert "limit caps matching messages returned" in text
    assert "limit caps messages scanned" not in text
    assert "independent SQLite work bound" in text
    assert "before registering sources" in text


def test_gmail_refresh_uses_bounded_scoped_commands_and_not_full_sync() -> None:
    """Gmail refresh stays bounded and never hides the broader sync pipeline."""
    text = (SKILL_ROOT / "ops" / "gmail-refresh.md").read_text(encoding="utf-8")
    commands = _fenced_commands(SKILL_ROOT / "ops" / "gmail-refresh.md")

    assert text
    assert "fieldkit doctor google --json" in commands
    assert "fieldkit gmail sync --since <YYYY-MM-DD> --max-messages <N> --json" in commands
    assert "fieldkit gmail account-tags --account <account> --json" in commands
    assert "fieldkit gmail enrich-pursuits --account <account> --json" in commands
    assert "fieldkit gmail sync --json" not in commands
    assert "fieldkit sync" not in commands
    assert "mailbox-wide" in text
    assert "first provider page" in text
    assert "not ready" in text
    assert "Stop on any non-zero status" in text
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in text


def test_gmail_refresh_documents_explicit_legacy_import_and_evidence_boundary() -> None:
    """Legacy data and credentialed runs retain their distinct proof boundaries."""
    text = (SKILL_ROOT / "ops" / "gmail-refresh.md").read_text(encoding="utf-8")
    commands = _fenced_commands(SKILL_ROOT / "ops" / "gmail-refresh.md")

    assert "fieldkit gmail import-cache --source <legacy-gmail.db> --db <fresh-managed-gmail.db> --json" in commands
    assert "owner-private verified snapshot" in text
    assert "source database and its sidecars unchanged" in text
    assert "fresh target" in text
    assert "configured and authorized" in text
    assert "manual release evidence" not in text
    assert "same final public candidate" not in text


@pytest.mark.parametrize(
    ("command", "options"),
    [
        (gmail_sync_cli, ("--db", "--max-messages", "--since", "--json")),
        (import_cache_cli, ("--source", "--db", "--json")),
    ],
)
def test_documented_gmail_cli_options_exist(command: click.Command, options: tuple[str, ...]) -> None:
    """The safe documented command forms are backed by the shipped adapters."""
    result = CliRunner().invoke(command, ["--help"])

    assert result.exit_code == 0
    for option in options:
        assert option in result.output


def test_ingest_evals_cover_scope_approval_and_partial_failure() -> None:
    """Static and behavioral checks exercise the public fail-closed contract."""
    raw = (SKILL_ROOT / "evals" / "evals.json").read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert raw
    assert "{{" not in raw
    case_ids = {case["id"] for case in payload["evals"]}
    check_ids = {check["id"] for check in payload["static_checks"]}
    assert {"scoped_transcript", "scoped_gmail", "gmail_partial_failure", "broad_sync_boundary"} <= case_ids
    assert {"account_scope", "approval_boundary", "no_secret_output_claim", "no_broad_sync_command"} <= check_ids
