"""Configuration prose has a behavior-backed, fixed-argv verification owner."""

import importlib
import json
from importlib import resources
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fieldkit.commands.companion.cli import cli as companion_cli
from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed
from fieldkit.companion.journal import journal_path
from fieldkit.companion.runner import run_action
from fieldkit.driver.github import AgentIssue
from fieldkit.driver.prompt_source import PromptSource, PromptSourceError
from fieldkit.util.bounded_process import BoundedProcessBytesResult
from scripts.documentation_commands import DOCUMENT_COMMANDS

pytestmark = pytest.mark.unit

_COMPANION_CLAIM = """Companion permission is not operator authorization or an operating-system
sandbox. `companion allowed` checks the configured gate without executing the
candidate command. `companion run` executes permitted commands and writes an
audit journal, including denied attempts. Default `companion feed` polling can
write a delivery cursor; `companion feed --json --all` avoids advancing it.
The gate admits feed inspection through its read-tier table only when `--all`
is an actual option, not an account value. Unknown feed options fail closed.
Quota reporting through the read-tier table rejects `--set`, `--period`,
`--data-root`, and unknown options. Selecting `--source sf` still performs
credentialed Salesforce reads; read permission does not mean offline execution.
Saved report selection requires `brief open --no-open` or
`pipeline open --no-open`; `--json` alone still launches a system viewer and is
not granted read-tier permission. Unknown report-selection options fail closed.
Approve the actual account/source scope and local or remote effects separately;
do not treat the `read` tier as a universal no-filesystem-write guarantee."""

# The fixed owner must also run the canonical driver prompt, ShadowBot URL,
# selected-account, and root/resource tests before these claims are approved.
_CONFIGURATION_SCOPES = (
    """The driver accepts `WorkOrder: docs/work-orders/<name>.md`, `OpenSpec:
openspec/changes/<name>/`, and `Speckit: specs/<name>/` issue references. A work
order carries its versioned `edit_sites` and `done_checks` contracts in
frontmatter. OpenSpec and Speckit directories use `driver.yaml` for both. The
driver validates every exact anchor
against the frozen `origin/main` revision before scheduling and creates the
execution worktree from that same commit. Missing or ambiguous anchors remain
non-passing; prose fences and line numbers are never inferred as authority.""",
    """`api_base` must be a public HTTPS URL with a path. fieldkit rejects local and
private literal addresses, non-default HTTPS ports, embedded credentials, query
parameters, and fragments before it sends an authorization bearer token.""",
    """An absent `accounts.yaml` means no optional account overrides. When a transcript
has action items to classify, task preparation requires a readable, valid mapping
if the file is present: malformed YAML, an invalid selected account entry, or
invalid attendee lists stop preparation rather
than silently changing task ownership. Brief and pipeline reports read
`config/accounts.yaml` in their selected workspace, including an explicit
`--data-root`, without falling back to a different workspace. When these reports
consult account configuration, invalid or duplicate content stops generation with
an invalid-data error. Other optional
configuration consumers may warn and use empty overrides; that fallback does not
apply to task preparation or these report reads.""",
    """Workspace and runtime-data roots must be absolute after `~` expansion. The optional
`fieldkit_root` override selects an application checkout and has the same requirement;
empty or invalid overrides are configuration errors. Skill discovery uses this
validated override when present and bundled package resources otherwise. Relative
paths passed to initialization are resolved before being stored in configuration.""",
)


def _assert_companion_claims(document: str) -> None:
    paragraphs = [" ".join(paragraph.split()) for paragraph in document.split("\n\n")]
    assert paragraphs.count(" ".join(_COMPANION_CLAIM.split())) == 1, "unreviewed companion scope"


def _configuration_document() -> str:
    return (Path(__file__).parents[1] / "docs/reference/config-file.md").read_text(encoding="utf-8")


def _assert_configuration_scopes(document: str) -> None:
    paragraphs = [" ".join(paragraph.split()) for paragraph in document.split("\n\n")]
    for claim in _CONFIGURATION_SCOPES:
        assert paragraphs.count(" ".join(claim.split())) == 1, "unreviewed configuration scope"


def test_configuration_driver_assistant_account_root_scope_is_reviewed() -> None:
    _assert_configuration_scopes(_configuration_document())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("same commit", "different commit"),
        ("Missing or ambiguous anchors remain\nnon-passing", "Missing or ambiguous anchors remain\npassing"),
        ("never inferred as authority", "inferred as authority"),
        ("rejects local and", "accepts local and"),
        ("before it sends an authorization bearer token", "after it sends an authorization bearer token"),
        ("stop preparation rather", "permit preparation rather"),
        ("without falling back to a different workspace", "by falling back to a different workspace"),
        ("invalid or duplicate content stops generation", "invalid or duplicate content permits generation"),
        ("must be absolute", "may be relative"),
        ("empty or invalid overrides are configuration errors", "empty or invalid overrides are accepted"),
        (
            "Relative\npaths passed to initialization are resolved",
            "Relative\npaths passed to initialization are not resolved",
        ),
        ("The driver accepts", "It is false that The driver accepts"),
        ("`api_base` must", "It is false that `api_base` must"),
        ("An absent `accounts.yaml` means", "It is false that An absent `accounts.yaml` means"),
        ("Workspace and runtime-data roots must", "It is false that Workspace and runtime-data roots must"),
    ],
)
def test_configuration_rejects_false_driver_assistant_account_root_scope(before: str, after: str) -> None:
    document = _configuration_document()
    assert before in document
    with pytest.raises(AssertionError, match="configuration scope"):
        _assert_configuration_scopes(document.replace(before, after))


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("git_succeeds", [False, True])
def test_documented_driver_freezes_origin_main_before_prompt_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, git_succeeds: bool
) -> None:
    """Run the actual driver/freeze path, stopping at the prompt-validation boundary."""
    import fieldkit.driver.runner as runner

    _assert_configuration_scopes(_configuration_document())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    work_order = repo / "docs/work-orders/example.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Fictional work order\n", encoding="utf-8")
    revision = "a" * 40
    git_calls: list[list[str]] = []

    def git_transport(argv: list[str], **_kwargs: object) -> BoundedProcessBytesResult:
        git_calls.append(argv)
        assert argv in (["git", "fetch", "origin", "main"], ["git", "rev-parse", "--verify", "origin/main^{commit}"])
        return BoundedProcessBytesResult(0 if git_succeeds else 1, (revision + "\n").encode(), b"")

    queue = Mock(return_value=[AgentIssue(1, "Fictional task", "WorkOrder: docs/work-orders/example.md", [])])
    validate = Mock(side_effect=PromptSourceError("stop before scheduling"))
    monkeypatch.delenv("FIELDKIT_DRIVER_SPEND_CAP", raising=False)
    monkeypatch.setattr(runner, "get_fieldkit_data", lambda: tmp_path / "data")
    monkeypatch.setattr(runner, "get_github_repo", lambda: "example/project")
    monkeypatch.setattr(runner, "list_ready_issues", queue)
    monkeypatch.setattr(runner, "freeze_prompt", validate)
    monkeypatch.setattr("fieldkit.driver.prompt_source.run_bounded_process_bytes", git_transport)

    result = runner.run_driver(repo_root=repo, dry_run=dry_run)

    assert result.outcome == "skipped"
    expected = [] if dry_run else [["git", "fetch", "origin", "main"]]
    if dry_run or git_succeeds:
        expected.append(["git", "rev-parse", "--verify", "origin/main^{commit}"])
    assert git_calls == expected
    if git_succeeds:
        queue.assert_called_once_with("example/project")
        validate.assert_called_once_with(repo, PromptSource(work_order, "work-order"), revision)
        assert "Prompt contract invalid or stale" in result.error
    else:
        queue.assert_not_called()
        validate.assert_not_called()
        assert "revision lookup failed" in result.error


def test_documented_skill_discovery_uses_bundled_resources_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fieldkit.commands.skill._runner as skill_runner
    import fieldkit.config._loader as config_loader

    _assert_configuration_scopes(_configuration_document())
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"fieldkit_home": str(tmp_path / "workspace")}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    bundled = Mock(wraps=resources.files)
    monkeypatch.setattr(resources, "files", bundled)
    config_loader.clear_config_caches()
    skill_runner._skills_dir.cache_clear()
    try:
        result = skill_runner._skills_dir()

        assert result.is_dir()
        bundled.assert_called_once_with("fieldkit.skills")
        assert (result / "companion/SKILL.md").read_text(encoding="utf-8").strip()
    finally:
        skill_runner._skills_dir.cache_clear()
        config_loader.clear_config_caches()


def test_configuration_companion_scope_is_reviewed() -> None:
    _assert_companion_claims(_configuration_document())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("is not operator authorization", "is operator authorization"),
        ("without executing", "by executing"),
        ("including denied attempts", "excluding denied attempts"),
        ("can\nwrite a delivery cursor", "cannot\nwrite a delivery cursor"),
        ("avoids advancing it", "advances it"),
        ("not an account value", "also an account value"),
        ("Unknown feed options fail closed", "Unknown feed options are accepted"),
        ("read-tier table rejects", "read-tier table accepts"),
        ("read permission does not mean offline execution", "read permission means offline execution"),
        ("still launches a system viewer", "never launches a system viewer"),
        ("not granted read-tier permission", "granted read-tier permission"),
        ("do not treat", "treat"),
        ("Companion permission is", "It is false that Companion permission is"),
    ],
)
def test_configuration_rejects_false_companion_scope(before: str, after: str) -> None:
    document = _configuration_document()
    assert before in document
    with pytest.raises(AssertionError, match="companion scope"):
        _assert_companion_claims(document.replace(before, after))


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["--all", "--json"], True),
        (["--account", "acme-corp", "--all"], True),
        (["--account", "--all"], False),
        (["--account=--all"], False),
        (["--all", "--future-option"], False),
    ],
)
def test_documented_feed_permission_parses_actual_options(tokens: list[str], expected: bool) -> None:
    _assert_companion_claims(_configuration_document())
    result = is_allowed(["companion", "feed", *tokens], "read", NO_ACT_POLICY)
    assert result is expected


@pytest.mark.parametrize("all_items", [False, True])
def test_documented_feed_cursor_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_items: bool) -> None:
    _assert_companion_claims(_configuration_document())
    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: data)

    result = CliRunner().invoke(companion_cli, ["feed", "--json", *(["--all"] if all_items else [])])

    assert result.exit_code == 0, result.output
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert items
    cursor = data / "companion-cursor.json"
    assert cursor.exists() is not all_items
    if not all_items:
        assert json.loads(cursor.read_text(encoding="utf-8"))["delivered"] == sorted(item["item_id"] for item in items)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "tokens", [["--set", "10"], ["--period", "2026-H2"], ["--data-root", "./other"], ["--future-option"]]
)
def test_documented_quota_read_permission_refuses_effect_options(tokens: list[str]) -> None:
    _assert_companion_claims(_configuration_document())
    result = is_allowed(["pipeline", "quota", *tokens], "read", NO_ACT_POLICY)
    assert result is False


def test_documented_read_tier_denial_still_writes_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_companion_claims(_configuration_document())
    monkeypatch.setattr(
        "fieldkit.companion.runner.subprocess.run", lambda *_args, **_kwargs: pytest.fail("denied action ran")
    )

    result = run_action(["pursuit", "advance", "acme/deal"], tier="read", policy=NO_ACT_POLICY, data_path=tmp_path)

    assert result.denied is True
    assert result.exit_code == 3
    record = json.loads(journal_path(tmp_path).read_text(encoding="utf-8"))
    assert record["exit_code"] == result.exit_code
    assert record["action"] == "pursuit advance acme/deal"


@pytest.mark.parametrize("group", ["brief", "pipeline"])
@pytest.mark.parametrize("no_open", [False, True])
def test_documented_json_report_viewer_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str, no_open: bool
) -> None:
    _assert_companion_claims(_configuration_document())
    module = importlib.import_module(f"fieldkit.commands.{group}.cli")
    reports = tmp_path / "briefs"
    reports.mkdir()
    prefix = "morning-brief" if group == "brief" else "pipeline-review"
    report = reports / f"{prefix}-2026-09-27.md"
    report.write_text("# Fictional report\n", encoding="utf-8")
    monkeypatch.setattr(module, "get_fieldkit_home", lambda: tmp_path)
    opened: list[str] = []

    def open_report(uri: str) -> bool:
        opened.append(uri)
        return True

    monkeypatch.setattr("webbrowser.open", open_report)
    tokens = ["--json", *(["--no-open"] if no_open else [])]

    permitted = is_allowed([group, "open", *tokens], "read", NO_ACT_POLICY)
    assert permitted is no_open
    result = CliRunner().invoke(module.cmd_open, tokens)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["opened"] is not no_open
    assert opened == ([] if no_open else [report.as_uri()])


@pytest.mark.parametrize("path", ["docs/reference/config-file.md", "docs/reference/environment-vars.md"])
def test_configuration_document_has_one_behavior_owner(path: str) -> None:
    root = Path(__file__).parents[1]
    contract = json.loads((root / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    owners = [name for name, owner in contract["verification"].items() if path in owner["paths"]]
    assert owners == ["configuration_contract"]
    assert DOCUMENT_COMMANDS[owners[0]] == (
        (
            "uv",
            "run",
            "pytest",
            "tests/test_configuration_document_owner.py",
            "tests/test_documentation_configuration_examples.py",
            "tests/test_config_xdg.py",
            "tests/test_companion_exact_permissions.py",
            "tests/test_companion_read_permissions.py",
            "tests/test_saved_report_viewers.py",
            "tests/test_driver_prompt_contract.py::test_freeze_prompt_reads_contract_and_target_from_same_revision",
            "tests/test_driver_prompt_contract.py::test_validate_edit_sites_rejects_unknown_or_ambiguous_anchor",
            "tests/test_driver_prompt_contract.py::test_freeze_openspec_requires_revision_bound_sidecar",
            "tests/test_driver.py::test_create_worktree_adds_from_frozen_revision",
            "tests/test_ingest_sync_action_items.py::test_classification_stops_before_classifier_on_invalid_accounts_file",
            "tests/test_ingest_sync_action_items.py::test_classification_rejects_malformed_account_ownership",
            "tests/test_ingest_sync_action_items.py::test_invalid_attendee_metadata_is_rejected",
            "tests/test_config_workspace_accounts.py::test_account_collector_binds_strict_read_to_its_workspace",
            "tests/test_config_workspace_accounts.py::test_pipeline_invalid_selected_accounts_stops_before_report_publication",
            "tests/test_config_workspace_accounts.py::test_account_collectors_propagate_invalid_selected_config",
            "tests/test_companion_gate.py::test_allowed_and_run_apply_same_tier_allowlist_policy",
            "tests/test_companion_runner.py::test_run_action_permitted_executes_and_journals",
            "tests/test_shadowbot_config.py::test_get_shadowbot_api_base_rejects_unsafe_destination",
            "tests/test_pipeline_quota.py::test_fetch_sf_closed_won_raises_auth_error_on_no_sid",
            "tests/test_pipeline_quota.py::test_selected_workspace_controls_resolution_write_and_query",
            "tests/test_pipeline_quota.py::test_quota_cli_source_sf_shows_territory_scoped_gap",
            "tests/test_skill_runner_dispatch.py::test_skills_dir_rejects_invalid_configured_root",
            "tests/test_skill_runner_dispatch.py::test_skills_dir_config_root_prefers_agents_skills_over_bare_skills",
            "tests/test_skill_runner_dispatch.py::test_skills_dir_importlib_resources_failure_is_fixed_config_error",
            "tests/test_config_source.py",
            "tests/test_init_wizard_funcs.py::test_minimal_cli_creates_only_generic_offline_scaffolding",
            "tests/test_configuration_reference_claims.py",
            "tests/test_forecast.py::test_forecast_auto_quota_auto_quota_from_config",
            "tests/test_driver_batch.py::test_batch_of_two_disjoint_issues_both_execute",
            "tests/test_driver_batch.py::test_batch_overlapping_second_issue_skipped_with_reason",
            "tests/test_contact_resolver.py::test_self_email_falls_back_to_configured_name",
            "tests/test_contact_resolver.py::test_self_email_case_insensitive_match",
            "tests/test_contact_enrich_pipeline.py::test_exclusion_guard_skip_user_own_email",
            "tests/test_contact_enrich_pipeline.py::test_enrich_batch_user_identity_contact_is_excluded",
            "tests/test_lib_config.py::test_get_user_name_returns_name_from_config",
            "tests/test_draft_queue.py::test_run_draft_queue_missing_endpoint_is_invalid_configuration",
            "tests/test_draft_queue.py::test_run_draft_queue_dry_run_returns_exit_0_no_mcp",
            "tests/test_watch_backstory_health.py::test_run_backstory_health_dry_run_does_not_read_accounts_or_provider",
            "tests/test_morning_brief.py::test_calendar_not_configured_is_explicitly_not_run",
            "tests/test_config_backward_compat.py::test_missing_canonical_workspace_key_raises_config_error",
            "tests/test_mcp_integration_config.py",
            "tests/test_watch_backstory_health.py::test_missing_backstory_endpoint_is_invalid_configuration",
            "tests/test_quota_period.py",
            "tests/test_driver.py::test_run_driver_limits_capped_execution_to_one_admitted_issue",
            "tests/test_dotenv_compat.py::test_load_dotenv_safe_preserves_existing_environment_by_default",
            "tests/test_gmail_sync.py::test_cached_token_needs_no_separate_client_settings",
            "tests/test_driver_admission.py::test_admit_fails_closed_when_run_budget_is_unset",
            "tests/test_driver_admission.py::test_admit_rejects_unreadable_or_exhausted_spend",
            "-q",
            "-n",
            "0",
        ),
    )
