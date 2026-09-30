"""Whole-page privacy ownership; local behavior is not provider privacy proof."""

import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.config._paths import get_harness_scratch_root
from fieldkit.protected_input import read_secret_file
from fieldkit.sqlite_read import _SnapshotConnection, open_sqlite_read_only
from tests.documentation_workflow_support import (
    deny_documentation_network as deny_documentation_network,
)
from tests.documentation_workflow_support import (
    documented_workspace as documented_workspace,
)
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow

pytestmark = pytest.mark.unit
PAGE = Path(__file__).resolve().parent.parent / "docs/privacy.md"
EXPECTED_BLOCKS: tuple[str, ...] = (
    "# Local data and privacy",
    "fieldkit is a single-user local application. It has no hosted fieldkit service and no shared\nmulti-tenant database. Your operating-system account is the application boundary.",
    "## What stays local",
    "The workspace, generated Markdown, configuration, caches, logs, and runtime databases are stored in\nthe configured workspace, data, and user-configuration roots. Disposable harness worktrees use\n`FIELDKIT_HARNESS_ROOT`, `$XDG_CACHE_HOME/fieldkit`, or `~/.cache/fieldkit`. None of these locations\nis bundled with the Python package or source repository. Keep configured roots outside your checkout.",
    "Treat these files as sensitive. Depending on the integrations you enable, they may contain customer\nnames, opportunity data, email content, meeting notes, prompts, and model responses. Do not commit\nthem to a public repository or attach them to an issue without redaction.",
    "Transcript ingest retains the rendered note, action items, and task classifications\nin local `pipeline.db` checkpoints until all required writes complete. Reprocessing\nan existing note separately retains its exact replacement text, destination, file\nmode, original content digest, and artifact identity until the replacement is\nverified and its database version is updated. Interrupted work keeps this content\nfor replay; deleting a checkpoint can destroy its saved decisions.",
    "Successful completion removes the corresponding checkpoint, but this is not a\nsecure-erasure guarantee. Continue treating the database, its SQLite sidecar files,\nand backups as sensitive. Note, pursuit, and task ownership comments contain\nidentity and content digests, not encryption. Keep them with their files so\nretries can recognize edited output. Follow the [pipeline recovery\nguide](guides/pipeline-workflow.md) to resolve interrupted work without discarding\nits recovery records.",
    "The verified SQLite snapshot reader does not open the original database through\nSQLite. It copies a verified, quiescent database while holding a shared lock that permits\nother readers but excludes updates. The copy is limited to 8 GiB, created as a\nmode-`0600` file inside a mode-`0700` system temporary directory, opened as an\nimmutable read-only database, and unlinked before the command receives the open\nconnection. Active writers are reported as retryable; rollback journals or a\nsnapshot that cannot be verified are rejected as invalid data. A source with\nmultiple hard links or a WAL-mode database header is also rejected: an alias can\nplace live WAL files under another basename that fieldkit cannot safely prove\nquiescent. Use the owning application's supported backup or export to produce a\nsingle-link, rollback-journal working copy before reading it with fieldkit.",
    "These are guarantees of that reader, not of every command labeled read-only.\nOptional automation spend accounting opens its external session database directly\nin SQLite read-only mode; it does not provide the same verified-snapshot contract.",
    "Handled failures, timeouts, controller loss, and termination remove partial\ncopies. An abrupt process death after a complete copy is written but before it is\nunlinked can still leave that bounded private file for the operating system or\noperator's temporary-file cleanup. This containment is not a secure-erasure\nguarantee.",
    "## What can leave the machine",
    "Commands using external APIs, provider clients, or authentication helpers can send requests\nbeyond the workstation. The table summarizes common destinations; authentication may also contact provider login and token endpoints.",
    "| Capability | Typical data destination |\n| --- | --- |\n| Salesforce | The Salesforce organization you configure |\n| Google | Gmail, Drive, Docs, or other enabled Google APIs |\n| LLM | The configured supported model provider |\n| MCP-backed tools | The configured MCP endpoint and its downstream services; Backstory authorization registers the People.ai MCP endpoint through MCPJungle |\n| GitHub-backed issue commands | The GitHub repository configured for fieldkit issues; issue titles, bodies, labels, and status changes |\n| Organization-provided services | The service endpoint configured by your operator; for example, a ShadowBot prompt, thread identifier, and returned response |",
    "The base installation and minimal first-success workflow require none of these integrations after\nthe package is installed.",
    "## Credentials",
    "Store credentials outside the source tree. fieldkit uses provider-supported tokens, local credential\nstores, or application-default credentials according to the selected integration. Never paste a\ncredential, session cookie, customer record, full email, or unredacted diagnostic bundle into a\npublic issue.",
    "The `auth sf --sid-file` and `auth shadowbot --refresh-token-file` options accept\nonly non-empty UTF-8 regular files with owner-only permissions (for example, mode `0600`).\nTheir limit is 8,192 bytes, including surrounding whitespace; symlinks and pipes\nare rejected. Input-reader errors omit the supplied path and file contents.",
    "Use the repository security policy for suspected vulnerabilities. Use the support path for setup\nquestions that do not contain sensitive data.",
)


def assert_privacy_guide(text: str) -> tuple[str, ...]:
    blocks = tuple(re.split(r"\n\s*\n", text.strip()))
    assert blocks == EXPECTED_BLOCKS, "privacy guide semantic inventory changed"
    return blocks


def test_complete_privacy_inventory() -> None:
    result = assert_privacy_guide(PAGE.read_text(encoding="utf-8"))
    assert result == EXPECTED_BLOCKS
    assert len(result) == len(BLOCK_OWNERS) == 18
    assert set(BLOCK_OWNERS) == set(EVIDENCE_NODES) | set(MANUAL_PENDING) | {"layout"}
    assert sum(block.startswith("|") for block in result) == 1
    assert not any(block.startswith("```") for block in result)
    assert all(
        node in PRIVACY_GUIDE_NODES or node.split("::", 1)[0] in PRIVACY_GUIDE_NODES
        for nodes in EVIDENCE_NODES.values()
        for node in nodes
    )


PRIVACY_GUIDE_NODES = (
    "tests/test_privacy_guide_contract.py",
    "tests/test_documentation_privacy_destinations.py",
    "tests/test_protected_input.py",
    "tests/test_sqlite_read_only.py",
    "tests/test_sqlite_snapshot_worker.py",
    "tests/test_config_xdg.py::test_config_and_cookie_paths_follow_absolute_xdg_root",
    "tests/test_config_xdg.py::test_relative_xdg_root_falls_back_to_home_config",
    "tests/test_init_guide_contract.py::test_real_minimal_init_and_preserving_rerun",
    "tests/test_ingest_prepared.py::test_prepared_output_round_trip_preserves_classified_tasks",
    "tests/test_ingest_prepared.py::test_prepared_path_rejects_symlink_escape",
    "tests/test_ingest_prepared.py::test_prepared_tasks_refuse_symlink_redirection",
    "tests/test_ingest_prepared_store.py::test_save_is_durable_and_identical_retry_does_not_write",
    "tests/test_ingest_prepared_store.py::test_completion_commits_artifact_status_and_checkpoint_together",
    "tests/test_ingest_prepared_store.py::test_completion_failure_retains_checkpoint_and_claim",
    "tests/test_reprocess_journal.py::test_reprocess_journal_commits_exact_intent",
    "tests/test_reprocess_journal.py::test_prepare_reprocess_saves_before_publication",
    "tests/test_reprocess_journal.py::test_reprocess_replay_applies_then_completes",
    "tests/test_reprocess_journal.py::test_reprocess_replay_refuses_changed_state",
    "tests/test_reprocess_journal.py::test_reprocess_completion_rolls_back_when_journal_delete_fails",
    "tests/test_reprocess_journal.py::test_reprocess_journal_refuses_descendant_alias",
    "tests/test_owned_markdown.py",
    "tests/test_ingest_db.py::test_get_db_path_falls_back_to_fieldkit_data_when_override_absent",
    "tests/test_ingest_db.py::test_get_db_path_rejects_override_outside_approved_roots",
)
# Pending classifications are reviewed text, not automated privacy guarantees.
BLOCK_OWNERS = (
    "layout",
    "manual-deployment-pending",
    "layout",
    "local-roots",
    "manual-redaction-pending",
    "retained-decisions",
    "completion-and-ownership",
    "snapshot",
    "spend-exception",
    "snapshot-cleanup",
    "layout",
    "manual-transfers-pending",
    "provider-boundaries-pending",
    "minimal-offline",
    "layout",
    "manual-credential-policy-pending",
    "protected-input",
    "manual-reporting-pending",
)
EVIDENCE_NODES = {
    "local-roots": (
        "tests/test_privacy_guide_contract.py::test_local_roots_and_scratch_are_isolated",
        "tests/test_privacy_guide_contract.py::test_relative_runtime_root_fails_without_writes",
        *PRIVACY_GUIDE_NODES[5:7],
        *PRIVACY_GUIDE_NODES[21:23],
    ),
    "retained-decisions": (
        *PRIVACY_GUIDE_NODES[8:12],
        PRIVACY_GUIDE_NODES[14],
        PRIVACY_GUIDE_NODES[15],
        PRIVACY_GUIDE_NODES[17],
        PRIVACY_GUIDE_NODES[19],
    ),
    "completion-and-ownership": (
        *PRIVACY_GUIDE_NODES[12:14],
        PRIVACY_GUIDE_NODES[16],
        PRIVACY_GUIDE_NODES[18],
        PRIVACY_GUIDE_NODES[20],
    ),
    "snapshot": (
        "tests/test_privacy_guide_contract.py::test_real_snapshot_is_private_unlinked_and_read_only",
        PRIVACY_GUIDE_NODES[3],
        PRIVACY_GUIDE_NODES[4],
    ),
    "snapshot-cleanup": (PRIVACY_GUIDE_NODES[3], PRIVACY_GUIDE_NODES[4]),
    "spend-exception": (
        "tests/test_documentation_privacy_destinations.py::test_optional_automation_spend_opens_original_session_database_read_only",
    ),
    "minimal-offline": (PRIVACY_GUIDE_NODES[7],),
    "protected-input": (
        "tests/test_privacy_guide_contract.py::test_documented_owner_only_file_and_whitespace_limit",
        PRIVACY_GUIDE_NODES[2],
    ),
    # Mock client routing proves construction only, never actual transfers,
    # endpoint ownership, provider retention, credentials, or supply-chain safety.
    "provider-boundaries-pending": (PRIVACY_GUIDE_NODES[1],),
}
MANUAL_PENDING = {
    "manual-deployment-pending": "Single-user deployment and OS-account boundary require operator review.",
    "manual-redaction-pending": "Sensitivity, redaction, backups, and publication require operator review.",
    "manual-transfers-pending": "Actual requests and login/token endpoints need separately authorized live review.",
    "provider-boundaries-pending": "Client-boundary tests are not live-transfer or downstream privacy proof.",
    "manual-credential-policy-pending": "Credential storage and selected authentication need integration review.",
    "manual-reporting-pending": "Security/support reporting and sensitive content require human review.",
}


@pytest.mark.parametrize("index", range(18), ids=BLOCK_OWNERS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_source_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    if mutation == "insert":
        blocks.insert(index, "<aside>Unreviewed privacy promise.</aside>")
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "alter":
        blocks[index] += " changed"
    elif mutation == "negate":
        blocks[index] = "Not " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_privacy_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "addition",
    (
        "[hidden]: https://example.com/private",
        "<!-- Unreviewed retention promise. -->",
        "```console\nfieldkit auth sf\n```",
        "[pipeline recovery guide][hidden]",
    ),
)
def test_unowned_appendices_and_reference_definitions_are_rejected(addition: str) -> None:
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_privacy_guide("\n\n".join(EXPECTED_BLOCKS) + "\n\n" + addition)


@pytest.mark.parametrize(
    "index,inline",
    [
        (index, value)
        for index, block in enumerate(EXPECTED_BLOCKS)
        for value in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", block)
    ],
)
def test_every_inline_value_mutation_is_rejected(index: int, inline: str) -> None:
    blocks = list(EXPECTED_BLOCKS)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`unsupported`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_privacy_guide("\n\n".join(blocks))


@pytest.mark.parametrize("row", range(8))
def test_every_destination_table_row_mutation_is_rejected(row: int) -> None:
    blocks = list(EXPECTED_BLOCKS)
    lines = blocks[12].splitlines()
    assert len(lines) == 8
    lines[row] += " unsupported"
    blocks[12] = "\n".join(lines)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_privacy_guide("\n\n".join(blocks))


@pytest.mark.parametrize("mutation", ("label", "target"))
def test_recovery_navigation_is_owned_and_resolves(mutation: str) -> None:
    label, target = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", EXPECTED_BLOCKS[6])[0]
    assert label == "pipeline recovery\nguide"
    assert target == "guides/pipeline-workflow.md"
    assert (PAGE.parent / target).is_file()
    blocks = list(EXPECTED_BLOCKS)
    replacement = f"[unsupported]({target})" if mutation == "label" else f"[{label}](unknown.md)"
    blocks[6] = blocks[6].replace(f"[{label}]({target})", replacement)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_privacy_guide("\n\n".join(blocks))


def test_local_roots_and_scratch_are_isolated(
    documented_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot_workflow(tmp_path)
    assert config.get_fieldkit_home() == documented_workspace
    assert config.get_fieldkit_data() == tmp_path / "runtime"
    monkeypatch.setenv("FIELDKIT_HARNESS_ROOT", str(tmp_path / "scratch"))
    assert get_harness_scratch_root() == tmp_path / "scratch"
    monkeypatch.delenv("FIELDKIT_HARNESS_ROOT")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert get_harness_scratch_root() == tmp_path / "cache" / "fieldkit"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert get_harness_scratch_root() == tmp_path / "home" / ".cache" / "fieldkit"
    assert snapshot_workflow(tmp_path) == before


def test_relative_runtime_root_fails_without_writes(
    documented_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = snapshot_workflow(tmp_path)
    monkeypatch.setenv("FIELDKIT_DATA_DIR", "relative-runtime")
    with pytest.raises(config_loader.ConfigError, match="absolute path"):
        config.get_fieldkit_data()
    assert config.get_fieldkit_home() == documented_workspace
    assert snapshot_workflow(tmp_path) == before


def test_real_snapshot_is_private_unlinked_and_read_only(tmp_path: Path) -> None:
    database = tmp_path / "fictional.db"
    with sqlite3.connect(database) as writer:
        writer.execute("CREATE TABLE example(value TEXT)")
        writer.execute("INSERT INTO example VALUES ('fictional')")
    writer.close()
    before = snapshot_workflow(tmp_path)
    connection = open_sqlite_read_only(database)
    try:
        assert isinstance(connection, _SnapshotConnection)
        assert connection._snapshot_fd is not None
        assert stat.S_IMODE(os.fstat(connection._snapshot_fd).st_mode) == 0o600
        row = connection.execute("SELECT value FROM example").fetchone()
        assert row["value"] == "fictional"
        snapshot = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        assert not snapshot.exists()
        assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO example VALUES ('forbidden')")
    finally:
        connection.close()
    assert not snapshot.parent.exists()
    assert snapshot_workflow(tmp_path) == before


def test_documented_owner_only_file_and_whitespace_limit(
    documented_workspace: Path,
    tmp_path: Path,
) -> None:
    assert documented_workspace.is_dir()
    secret = tmp_path / "fictional-input"
    secret.write_text(" " + "fictional".ljust(8190) + "\n", encoding="utf-8")
    secret.chmod(0o600)
    result = read_secret_file(secret, label="credential")
    assert result == "fictional"
    secret.write_bytes(secret.read_bytes() + b" ")
    before = snapshot_workflow(tmp_path)
    outcome = invoke_workflow(["auth", "sf", "--sid-file", str(secret)])
    assert outcome.exit_code == 3
    assert "8192 bytes" in outcome.stderr
    assert str(secret) not in outcome.output
    assert "fictional" not in outcome.output
    assert snapshot_workflow(tmp_path) == before
