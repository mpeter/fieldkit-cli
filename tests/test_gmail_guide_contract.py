"""Whole-page literal inventory and fixed local Gmail guide evidence manifest."""

import ast
import re
from pathlib import Path

import pytest

from fieldkit.__main__ import main
from fieldkit.gmail.publication import initialize_gmail_publication
from tests.documentation_workflow_support import snapshot_workflow

GUIDE = Path("docs/guides/gmail.md")
OWN_MODULE = "tests/test_gmail_guide_contract.py"
MANIFEST = (
    OWN_MODULE,
    "tests/test_gmail_sync_selected_mode.py",
    "tests/test_smoke_gmail_examples.py::test_documentation_account_tag_transcript",
    "tests/test_gmail_enrich_pursuits_json.py::test_json_reports_written_and_skipped_accounts",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_json_preserves_suspect_without_consuming_limit",
    "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
    "tests/test_smoke_gmail_examples.py::test_documentation_import_executes_fixed_legacy_cache_example",
    "tests/test_smoke_gmail_examples.py::test_documentation_import_rejects_occupied_destination_without_writes",
    "tests/test_smoke_gmail_examples.py::test_documentation_import_cli_rejects_copied_managed_source",
    "tests/test_gmail_cache_import.py::test_preview_rejects_unknown_schema_without_creating_a_target",
    "tests/test_gmail_sync_coverage.py::test_negative_max_messages_exits_three_before_auth_or_db",
    "tests/test_gmail_refresh_input_selection.py::test_import_to_different_target_preserves_selection_for_doctor_tags_and_enrichment",
    "tests/test_gmail_refresh_input_selection.py::test_valid_account_without_pursuits_skips_without_creating_or_replacing_report",
    "tests/test_enrich_pursuits.py::test_build_account_report_no_db_build_account_report_calls_direct_functions",
    "tests/test_enrich_pursuits.py::test_review_checklist_routes_email_evidence_to_read_only_native_coaching",
    "tests/test_enrich_pursuits.py::test_build_account_report_sets_aside_suspected_masked_contacts",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_sets_aside_suspected_masked_address_by_default",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_include_suspected_marks_address",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_missing_domains_preserves_matching_shape",
    "tests/test_gmail_address_quality.py::test_is_suspected_masked_address",
)

REPORT_AUTHORITIES = (
    "tests/test_gmail_refresh_input_selection.py::test_import_to_different_target_preserves_selection_for_doctor_tags_and_enrichment",
    "tests/test_gmail_refresh_input_selection.py::test_valid_account_without_pursuits_skips_without_creating_or_replacing_report",
    "tests/test_enrich_pursuits.py::test_build_account_report_no_db_build_account_report_calls_direct_functions",
    "tests/test_enrich_pursuits.py::test_review_checklist_routes_email_evidence_to_read_only_native_coaching",
)
BLINDSPOT_AUTHORITIES = (
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_sets_aside_suspected_masked_address_by_default",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_include_suspected_marks_address",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_json_preserves_suspect_without_consuming_limit",
    "tests/test_gmail_query_cli_commands.py::test_cmd_blindspots_missing_domains_preserves_matching_shape",
    "tests/test_gmail_address_quality.py::test_is_suspected_masked_address",
    "tests/test_enrich_pursuits.py::test_build_account_report_sets_aside_suspected_masked_contacts",
)

# Each literal is one ordered nonblank source block, including Markdown syntax.
INVENTORY = (
    (
        0,
        "manual: editorial or live operator authority pending",
        "---\nlast_reviewed: 2026-09-27\ncovers:\n  - src/fieldkit/commands/gmail/\n  - src/fieldkit/gmail/\n  - src/fieldkit/config/_integrations.py\naudience: ae-user\n---",
    ),
    (1, "manual: editorial or live operator authority pending", "# Gmail Sync"),
    (2, "manual: editorial or live operator authority pending", "## Prerequisites"),
    (
        3,
        "manual: editorial or live operator authority pending",
        "- `fieldkit init` has been completed (run `fieldkit doctor` to confirm)\n- Your Google account is authenticated (OAuth token exists — see first-time setup below)",
    ),
    (
        4,
        "manual: editorial or live operator authority pending",
        "Provider sync requires the `google` installation profile; see\n[installation and first success](../getting-started.md). Local cache queries and `import-cache` do not\nrequire that profile or contact Google.",
    ),
    (5, "manual: editorial or live operator authority pending", "## First-time OAuth setup"),
    (
        6,
        "manual: editorial or live operator authority pending",
        "Before the first sync, configure `GOOGLE_OAUTH_CLIENT_ID` and\n`GOOGLE_OAUTH_CLIENT_SECRET` for your Google OAuth application. The shared consent\nflow requests Gmail read-only plus Google Docs and Google Drive read/write access,\nincluding when started for Gmail alone. Review these permissions before granting\naccess; fieldkit does not select scopes per command. From an interactive terminal,\nrun:",
    ),
    (7, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync\n```"),
    (
        8,
        "manual: editorial or live operator authority pending",
        "If no OAuth token exists, fieldkit prints an authorization URL to the terminal. Open that\nURL in your browser, grant access when prompted, and fieldkit stores the token at\nthe configured `gmail_token` path, defaulting to\n`<fieldkit_data>/google-oauth-token.json`. A valid saved token is reused, and an expired\ntoken with a refresh token is renewed automatically. These saved-token paths do not\nrequire duplicate OAuth client settings in the environment. Revoked or unusable\ncredentials require authorization again.",
    ),
    (
        9,
        "manual: editorial or live operator authority pending",
        "> **Note:** `fieldkit gmail sync` must be run in an interactive terminal for the initial\n> OAuth flow. If run from a systemd timer, cron job, or any non-interactive context with\n> no cached token, it exits with code 2 and a message directing you to run it manually\n> first. Complete the browser consent flow once, and subsequent automated runs will use\n> the cached token without prompting.",
    ),
    (10, "manual: editorial or live operator authority pending", "## Sync modes"),
    (11, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync\n```"),
    (
        12,
        "manual: editorial or live operator authority pending",
        "With no selection flag, fieldkit automatically chooses the safe mode from the local\ndatabase state. A complete database uses Gmail history for an incremental sync. A new or\nincomplete database starts or resumes a full sync. Duration depends on mailbox\nvolume, provider response times, and rate limits.",
    ),
    (13, "manual: editorial or live operator authority pending", "To deliberately refetch the entire mailbox, run:"),
    (14, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync --full\n```"),
    (
        15,
        "manual: editorial or live operator authority pending",
        "This is a resumable upsert refresh. It starts at the first Gmail message page, preserves\nits page checkpoint if interrupted, and reconciles mailbox changes that occur during the\nscan before marking the database complete. It refreshes rows returned by Gmail; it does\nnot remove cached rows merely because they were absent from the full listing.",
    ),
    (
        16,
        "manual: editorial or live operator authority pending",
        "To backfill from a known UTC date without changing automatic sync continuity, run:",
    ),
    (17, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync --since 2026-06-01\n```"),
    (
        18,
        "manual: editorial or live operator authority pending",
        "`--since` includes messages at UTC midnight on the selected date and resumes from its\nown saved page checkpoint after interruption. It upserts matching messages and leaves\nthe full/incremental history state untouched. It cannot be combined with `--full`.",
    ),
    (
        19,
        "manual: editorial or live operator authority pending",
        "By default, progress and status go to the log rather than stdout. Pass `--json`\nto print the sync outcome on stdout, including counts and retry status. To see\nongoing sync activity, check your system logs or run with verbose logging enabled.",
    ),
    (20, "manual: editorial or live operator authority pending", "Optional flags:"),
    (
        21,
        "manual: editorial or live operator authority pending",
        "- `--db PATH` — override the path to `gmail.db`\n- `--full` — force a resumable full-mailbox refresh\n- `--since YYYY-MM-DD` — run an isolated, resumable backfill on or after a UTC date\n- `--json` — print the final sync outcome as JSON on stdout\n- `--max-messages N` — limit the full or date-bounded message scan to a cumulative,\n  nonnegative number of processed messages (`0` means unlimited). Retry with a larger\n  value or `0` to continue from the saved page checkpoint. This does not bound an\n  incremental history sync or the history replay that finishes any completed full\n  scan, including the initial scan.",
    ),
    (
        22,
        "manual: editorial or live operator authority pending",
        "For a bounded rehearsal, use a fresh isolated cache and isolated configuration with\nan owner-only copy of the credential. A different `--db` path alone does not isolate\ncredentials: refreshing OAuth can rewrite the configured token file. Apply an overall\ntimeout in addition to a message limit, and verify the selected sync mode and persisted\ncounts rather than treating the option as a universal resource bound.",
    ),
    (23, "manual: editorial or live operator authority pending", "## Apply intel after sync"),
    (
        24,
        "manual: editorial or live operator authority pending",
        "After syncing, run these two commands to push Gmail signals into your workspace:",
    ),
    (25, "manual: editorial or live operator authority pending", "**Tag threads by account:**"),
    (
        26,
        "tests/test_smoke_gmail_examples.py::test_documentation_account_tag_transcript",
        "```\nfieldkit gmail account-tags\n```",
    ),
    (
        27,
        "tests/test_smoke_gmail_examples.py::test_documentation_account_tag_transcript",
        "This scans your synced messages for Gmail labels matching the `ref/*` pattern\n(e.g. `ref/acme-bank`) and maps each thread to its account in the database.\nLabels must be configured in Gmail as filters — fieldkit does not read\n`accounts.yaml` domain entries for this step.",
    ),
    (
        28,
        "tests/test_smoke_gmail_examples.py::test_documentation_account_tag_transcript",
        "For a fictional cache with two threads labeled `ref/acme-corp` and one labeled\n`ref/global-pay`, the output is:",
    ),
    (
        29,
        "tests/test_smoke_gmail_examples.py::test_documentation_account_tag_transcript",
        "```\nUpserted 3 thread-account associations across 2 account(s):\n  acme-corp: 2 threads\n  global-pay: 1 threads\n```",
    ),
    (30, "manual: editorial or live operator authority pending", "**Generate Gmail intelligence reports:**"),
    (
        31,
        "tests/test_gmail_enrich_pursuits_json.py::test_json_reports_written_and_skipped_accounts",
        "```\nfieldkit gmail enrich-pursuits\n```",
    ),
    (
        32,
        REPORT_AUTHORITIES,
        "This reads your synced Gmail database and writes a `gmail-intel.md` report to each\nselected configured account that has pursuit Markdown files\n(`accounts/<account>/gmail-intel.md`). Accounts without pursuits are skipped. Each report\ncontains top contacts by email volume, champion signals, per-pursuit thread matches,\nand a review checklist. It does **not** modify pursuit frontmatter files.",
    ),
    (33, "manual: editorial or live operator authority pending", "Optional flags:"),
    (34, (REPORT_AUTHORITIES[0],), "- `--account SLUG` — generate the report for one account only"),
    (35, "manual: editorial or live operator authority pending", "## Review blindspot contacts"),
    (
        36,
        BLINDSPOT_AUTHORITIES,
        "Use `fieldkit gmail query blindspots ACCOUNT` to find active contacts in an\naccount's tagged threads. By default, the command sets aside addresses that match\na narrow masked-data signal: the address uses one of the account's configured\ndomains and its name-like local part ends in a dot-separated, four-character\nlowercase ASCII letter/digit token containing at least one letter. Numeric endings\nsuch as `.2026` remain ordinary. The command reports the number set aside; pass\n`--include-suspected` to inspect them, visibly marked as `SUSPECTED`.",
    ),
    (
        37,
        BLINDSPOT_AUTHORITIES,
        "JSON output keeps actionable records in `items` and exposes set-aside records in\n`suspected_items`, with `suspected_count` and a stable `quality_reason`. Accounts\nwithout configured domains retain their existing results because fieldkit does not\nguess a domain from the account slug. Generated `gmail-intel.md` reports apply the\nsame classification and state how many addresses were set aside.",
    ),
    (38, "manual: editorial or live operator authority pending", "## Check sync health"),
    (
        39,
        "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
        "Check that the configured managed cache is verified and ready for local queries:",
    ),
    (
        40,
        "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
        "```\nfieldkit doctor gmail\n```",
    ),
    (
        41,
        "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
        "This resolves the configured `gmail_db` path. Pass `--db PATH` to inspect a\ndifferent cache. Exit `0` means the verified managed snapshot is query-ready and\nhas the required tables. Exit `1` means an active writer, a resource limit, or a\nnot-ready cache makes the check retryable; wait for sync to finish or resolve the\nreported resource problem. Exit `3` means invalid or unverified local data needs\nattention. This check does not establish mailbox\nfreshness or perform a complete SQLite integrity check. Run `fieldkit gmail sync`\nwhen you need to refresh cached messages.",
    ),
    (42, "manual: editorial or live operator authority pending", "## Common errors"),
    (43, "manual: editorial or live operator authority pending", "**OAuth authorization revoked or token unusable:**"),
    (
        44,
        "manual: editorial or live operator authority pending",
        "Ordinary access-token expiry is handled automatically. If authorization has been\nrevoked or the saved token cannot be used, first restore your OAuth client settings.\nIf reauthorization is necessary, stop jobs using Google credentials and identify\nthe effective `gmail_token` path in your configuration. Move that file to a\nprivate, owner-only backup location before retrying in an interactive terminal.\nDo not guess its path or post its contents. The token is shared with other Google\ncommands, so they may also need authorization again. With the unusable token\nsafely set aside, fieldkit prints an authorization URL:",
    ),
    (45, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync\n```"),
    (
        46,
        "manual: editorial or live operator authority pending",
        'If you are running sync on a schedule (systemd timer, cron) and see exit 2 with a "not a\nTTY" message, run `fieldkit gmail sync` manually first to complete the consent flow, then\nyour automated runs will resume normally.',
    ),
    (47, "manual: editorial or live operator authority pending", "**Database not found:**"),
    (48, "manual: editorial or live operator authority pending", "The database has not been created yet. Run:"),
    (49, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync\n```"),
    (
        50,
        "manual: full completion and provider history continuity pending",
        "This starts a full sync from scratch. Subsequent runs use incremental history only\nafter the database has completed the full scan and its history reconciliation.",
    ),
    (51, "manual: editorial or live operator authority pending", "**Database unreadable or missing required tables:**"),
    (
        52,
        "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
        "```\nfieldkit doctor gmail\n```",
    ),
    (
        53,
        "tests/test_smoke_gmail_examples.py::test_documentation_gmail_doctor_retries_until_published_cache_is_query_ready",
        "Check the effective `gmail_db` path and its permissions first. An unreadable cache\nis not necessarily corrupt. Do not build missing tables or edit publication\nidentity metadata by hand.",
    ),
    (
        54,
        "manual: same-filesystem operator backup and restore pending",
        "Before rebuilding, stop every reader, writer, scheduled job, and other process\nusing the cache. Create an unused, owner-only backup directory on the same\nfilesystem. Rename the original database, any adjacent `-wal` and `-shm` files,\nand its complete sibling publication directory into that directory as one\npreserved group. For `gmail.db`, the publication directory is\n`gmail.db.publication`. Keep the group private: it contains email data.\nSame-filesystem renames preserve the original database inode; copies do not\nguarantee a writable restoration of a managed publication. Never overwrite an\nexisting backup or try to rebind its identity.",
    ),
    (
        55,
        "manual: editorial or live operator authority pending",
        "Confirm both the original database path and publication-directory path are absent\n(not dangling symlinks), with no sidecars left behind, before creating a new cache\nat the configured path:",
    ),
    (56, "manual: editorial or live operator authority pending", "```\nfieldkit gmail sync\n```"),
    (
        57,
        "manual: operator rollback pending",
        "Retain the group until sync completes and `fieldkit doctor gmail` verifies the\nreplacement. To roll back, stop all cache users again. Rename the complete\nreplacement group into a different unused private directory on the same\nfilesystem, then rename the original database, saved sidecars, and complete\npublication directory back to their exact original paths. Do not merge groups,\nrestore only the database, or hand-edit identities. Verify with doctor before\nrestarting users.",
    ),
    (58, "manual: editorial or live operator authority pending", "## Import a supported legacy cache"),
    (
        59,
        "tests/test_smoke_gmail_examples.py::test_documentation_import_executes_fixed_legacy_cache_example",
        "Use `import-cache` only for a supported legacy Gmail schema, not a managed backup.\nA managed database contains `_fieldkit_publication` and is rejected as an import\nsource. Restore a managed backup by the same-filesystem group rename described\nabove instead.",
    ),
    (
        60,
        "tests/test_smoke_gmail_examples.py::test_documentation_import_executes_fixed_legacy_cache_example",
        "Stop users of the legacy source and retain it unchanged. Choose a fresh target\nwhose real parent directory already exists; its database, sidecars, and sibling\npublication directory must not exist. First validate without creating the target,\nthen import and check the new managed cache:",
    ),
    (
        61,
        "tests/test_smoke_gmail_examples.py::test_documentation_import_executes_fixed_legacy_cache_example",
        "```console\nfieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db --dry-run\nfieldkit gmail import-cache --source ./legacy-gmail.db --db ./managed/gmail.db\nfieldkit doctor gmail --db ./managed/gmail.db\n```",
    ),
    (
        62,
        "tests/test_smoke_gmail_examples.py::test_documentation_import_executes_fixed_legacy_cache_example",
        "Unknown schemas, ambiguous or changing inputs, and occupied destinations fail\nclosed. Preview success is not an import: the actual command validates again.\nNeither command adopts or modifies the legacy source in place.\n",
    ),
)
EXPECTED = tuple(block for _, _, block in INVENTORY)


def assert_inventory(source: str) -> None:
    assert tuple(source.split("\n\n")) == EXPECTED, "Gmail whole-page inventory changed"


def assert_authorities(inventory: tuple[tuple[int, str | tuple[str, ...], str], ...]) -> None:
    expected = {
        32: REPORT_AUTHORITIES,
        34: (REPORT_AUTHORITIES[0],),
        36: BLINDSPOT_AUTHORITIES,
        37: BLINDSPOT_AUTHORITIES,
    }
    assert {index: authority for index, authority, _ in inventory if index in expected} == expected


@pytest.mark.unit
def test_complete_ordered_source_inventory_and_fixed_authority() -> None:
    source = GUIDE.read_text(encoding="utf-8")
    assert_inventory(source)
    assert len(INVENTORY) == 63
    assert all(block.strip() for block in EXPECTED)
    assert MANIFEST.count(OWN_MODULE) == 1
    assert len(MANIFEST) == len(set(MANIFEST))
    assert_authorities(INVENTORY)
    for _, authority, _ in INVENTORY:
        for selector in authority if isinstance(authority, tuple) else (authority,):
            if selector.startswith("manual: "):
                continue
            assert selector in MANIFEST
            file_name, function = selector.split("::")
            source_text = Path(file_name).read_text(encoding="utf-8")
            tree = ast.parse(source_text)
            matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
            assert len(matches) == 1
            body = ast.get_source_segment(source_text, matches[0])
            assert body is not None and "assert " in body


@pytest.mark.unit
@pytest.mark.parametrize("index", (32, 34, 36, 37))
def test_report_and_blindspot_authority_drop_or_misroute_rejected(index: int) -> None:
    inventory: list[tuple[int, str | tuple[str, ...], str]] = list(INVENTORY)
    authority = inventory[index][1]
    assert isinstance(authority, tuple)
    inventory[index] = (index, authority[:-1], inventory[index][2])
    with pytest.raises(AssertionError):
        assert_authorities(tuple(inventory))
    inventory[index] = (index, (MANIFEST[1],), inventory[index][2])
    with pytest.raises(AssertionError):
        assert_authorities(tuple(inventory))


@pytest.mark.unit
@pytest.mark.parametrize("index", range(len(INVENTORY)))
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "duplicate", "swap"))
def test_each_source_block_mutation_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED)
    if mutation == "insert":
        blocks.insert(index, "<aside>Unreviewed Gmail claim.</aside>")
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "alter":
        blocks[index] += " changed"
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    else:
        blocks[index], blocks[(index + 1) % len(blocks)] = blocks[(index + 1) % len(blocks)], blocks[index]
    with pytest.raises(AssertionError, match="whole-page inventory"):
        assert_inventory("\n\n".join(blocks))


FENCES = tuple((i, line) for i, block in enumerate(EXPECTED) for line in block.splitlines() if line.startswith("```"))
INLINE = tuple((i, value) for i, block in enumerate(EXPECTED) for value in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", block))
LINKS = tuple(
    (i, label, target)
    for i, block in enumerate(EXPECTED)
    for label, target in re.findall(r"\[([^]]+)\]\(([^)]+)\)", block)
)


@pytest.mark.unit
@pytest.mark.parametrize("index,line", FENCES)
def test_fence_mutation_rejected(index: int, line: str) -> None:
    blocks = list(EXPECTED)
    blocks[index] = blocks[index].replace(line, line + "changed", 1)
    with pytest.raises(AssertionError, match="whole-page inventory"):
        assert_inventory("\n\n".join(blocks))


@pytest.mark.unit
@pytest.mark.parametrize("index,value", INLINE)
def test_inline_mutation_rejected(index: int, value: str) -> None:
    blocks = list(EXPECTED)
    blocks[index] = blocks[index].replace(f"`{value}`", "`changed`", 1)
    with pytest.raises(AssertionError, match="whole-page inventory"):
        assert_inventory("\n\n".join(blocks))


@pytest.mark.unit
@pytest.mark.parametrize("index,label,target", LINKS)
@pytest.mark.parametrize("part", ("label", "target"))
def test_link_mutation_rejected(index: int, label: str, target: str, part: str) -> None:
    blocks = list(EXPECTED)
    replacement = f"[changed]({target})" if part == "label" else f"[{label}](changed.md)"
    blocks[index] = blocks[index].replace(f"[{label}]({target})", replacement, 1)
    with pytest.raises(AssertionError, match="whole-page inventory"):
        assert_inventory("\n\n".join(blocks))


@pytest.mark.unit
@pytest.mark.parametrize(
    "index,needle", ((0, "last_reviewed"), (0, "covers:"), (4, "../getting-started.md"), (61, "```console"))
)
def test_hidden_reference_mutation_rejected(index: int, needle: str) -> None:
    blocks = list(EXPECTED)
    assert needle in blocks[index]
    blocks[index] = blocks[index].replace(needle, needle + "changed", 1)
    with pytest.raises(AssertionError, match="whole-page inventory"):
        assert_inventory("\n\n".join(blocks))


@pytest.mark.integration
def test_doctor_guide_local_cache_readiness_and_no_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workflow = tmp_path / "workflow"
    workflow.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "retained.txt").write_text("Retained fictional target\n", encoding="utf-8")
    (workflow / "existing-alias").symlink_to(external, target_is_directory=True)
    external_before = snapshot_workflow(external)
    database = workflow / "gmail.db"
    before_missing = snapshot_workflow(workflow)
    missing = main(["doctor", "gmail", "--db", str(database)])
    assert missing == 3
    assert not database.exists()
    assert snapshot_workflow(workflow) == before_missing
    assert snapshot_workflow(external) == external_before
    assert "gmail" in capsys.readouterr().out

    receipt = initialize_gmail_publication(database)
    assert receipt.generation == 1
    before = snapshot_workflow(workflow)
    status = main(["doctor", "gmail", "--db", str(database)])
    assert status == 1
    assert "not ready" in capsys.readouterr().out.lower()
    assert snapshot_workflow(workflow) == before
    assert snapshot_workflow(external) == external_before


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ("rename", "mode", "symlink", "tree", "external"))
def test_doctor_snapshot_detects_filesystem_mutation(tmp_path: Path, mutation: str) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same.txt").write_text("same", encoding="utf-8")
    (second / "same.txt").write_text("same", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "retained.txt").write_text("same", encoding="utf-8")
    (first / "existing-alias").symlink_to(external, target_is_directory=True)
    before = snapshot_workflow(first)
    external_before = snapshot_workflow(external)
    if mutation == "rename":
        (first / "same.txt").rename(first / "renamed.txt")
    elif mutation == "mode":
        (first / "same.txt").chmod(0o600)
    elif mutation == "symlink":
        (first / "link.txt").symlink_to("same.txt")
    elif mutation == "tree":
        (first / "nested").mkdir()
    else:
        (first / "existing-alias" / "retained.txt").write_text("changed", encoding="utf-8")
        assert snapshot_workflow(first) == before
        assert snapshot_workflow(external) != external_before
        return
    assert snapshot_workflow(first) != before
