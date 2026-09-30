"""Reviewed residual reference prose; behavior links are not whole-page approval.

Provider billing, secret storage, backups/reinstallation, administrator approval,
and shell export semantics remain advisory/manual claims, not executable proof.
Companion, driver authority, assistant URL policy, strict accounts, resource roots,
doctor, and log-root paragraphs have existing owners and are excluded here.
"""

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import dotenv.main
import pytest
import yaml
from google_auth_oauthlib.flow import InstalledAppFlow

import fieldkit.__main__ as main_module
import fieldkit.config as config_api
import fieldkit.config._loader as config_loader
import fieldkit.watch.backstory_health as backstory_health
import fieldkit.watch.mcp as watcher_mcp
from fieldkit.__main__ import main
from fieldkit.commands.init.wizard import _wizard_write_artifacts
from fieldkit.config import (
    ConfigError,
    clear_config_caches,
    get_companion_act_allowlist,
    get_companion_tier,
    get_fieldkit_data,
    get_github_repo,
    get_llm_model,
    get_mcp_endpoint,
    get_mcp_gateway_url,
    get_sf_rest_base_url,
    get_shadowbot_api_base,
)
from fieldkit.config._settings import get_vertex_location
from fieldkit.driver import admission, spend
from fieldkit.gmail import auth as gmail_auth
from fieldkit.pipeline.collect import collect_blindspot_data
from fieldkit.shadowbot.client import ShadowbotClient
from scripts.markdown_tables import parse_markdown_tables

pytestmark = pytest.mark.unit

_CONFIG = Path("docs/reference/config-file.md")
_ENVIRONMENT = Path("docs/reference/environment-vars.md")
_EXAMPLES = "tests/test_documentation_configuration_examples.py::"


@dataclass(frozen=True)
class ReferenceClaim:
    document: Path
    anchor: str
    paragraph: str
    before: str
    after: str
    behavior: tuple[str, ...] = ()


_CLAIMS = (
    ReferenceClaim(
        _CONFIG,
        "When `XDG_CONFIG_HOME`",
        "When `XDG_CONFIG_HOME` is an absolute path, fieldkit instead uses "
        "`$XDG_CONFIG_HOME/fieldkit/config.yaml`. The Salesforce cookie file follows the "
        "same directory. Relative XDG paths are ignored, as required by the XDG base "
        "directory specification.",
        "Relative XDG paths are ignored",
        "Relative XDG paths are accepted",
        (
            "tests/test_config_xdg.py::test_config_and_cookie_paths_follow_absolute_xdg_root",
            "tests/test_config_xdg.py::test_relative_xdg_root_falls_back_to_home_config",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "Minimal initialization",
        "Minimal initialization creates a generic workspace without identity values:",
        "without identity values",
        "with discovered identity values",
        (
            "tests/test_configuration_reference_claims.py::test_minimal_initialization_omits_identity_and_defaults_runtime",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "Workspace identity written",
        "Workspace identity written by interactive setup lives separately at "
        "`<fieldkit_home>/config/identity.yaml`. Its `territory` and "
        "`salesforce_user_id` values are template metadata, not global integration "
        "settings. Setup also records the supplied name, email, role, and company there "
        "for workspace-owned templates.",
        "not global integration settings",
        "global integration settings",
        ("tests/test_configuration_reference_claims.py::test_interactive_setup_retains_workspace_identity_metadata",),
    ),
    ReferenceClaim(
        _CONFIG,
        "`fieldkit_home` is the required",
        "`fieldkit_home` is the required workspace-root key. If it is absent, commands "
        "that need the workspace fail with exit `3`; run `fieldkit init` or "
        "`fieldkit init --minimal PATH` to write a current configuration.",
        "exit `3`",
        "exit `0`",
        (
            "tests/test_config_backward_compat.py::test_missing_canonical_workspace_key_raises_config_error",
            "tests/test_configuration_reference_claims.py::test_workspace_command_missing_home_exits_three",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "Installing an optional profile",
        "Installing an optional profile does not populate any of these keys or grant "
        "service access. See [Integrations and profiles](../integrations.md).",
        "does not populate",
        "automatically populates",
    ),
    ReferenceClaim(
        _CONFIG,
        "The three `mcp_endpoints`",
        "The three `mcp_endpoints` values are independent, optional capabilities. fieldkit "
        "does not derive private route names or append organization-specific paths. Supply "
        "the complete endpoint exactly as provided by an operator you trust:",
        "does not derive",
        "automatically derives",
        (
            "tests/test_mcp_integration_config.py::test_mcp_endpoint_is_disabled_when_not_configured",
            "tests/test_mcp_integration_config.py::test_mcp_endpoint_returns_exact_operator_supplied_url",
            "tests/test_configuration_reference_claims.py::test_all_optional_mcp_getters_omit_or_reject_unsafe_endpoints",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "Endpoints must use",
        "Endpoints must use HTTP or HTTPS and cannot contain embedded credentials, query "
        "parameters, or fragments. Leave a capability absent when you do not have that "
        "service; local aggregate workflows report it as not run. Explicitly selecting a "
        "live `backstory-health` or `draft-queue` watcher without its required endpoint is "
        "invalid configuration and exits `3`. Their dry runs do not require provider "
        "access.",
        "cannot contain embedded credentials",
        "may contain embedded credentials",
        (
            "tests/test_mcp_integration_config.py::test_mcp_endpoint_rejects_unsafe_or_ambiguous_urls",
            "tests/test_watch_backstory_health.py::test_missing_backstory_endpoint_is_invalid_configuration",
            "tests/test_draft_queue.py::test_run_draft_queue_missing_endpoint_is_invalid_configuration",
            "tests/test_watch_backstory_health.py::test_run_backstory_health_dry_run_does_not_read_accounts_or_provider",
            "tests/test_draft_queue.py::test_run_draft_queue_dry_run_returns_exit_0_no_mcp",
            "tests/test_morning_brief.py::test_calendar_not_configured_is_explicitly_not_run",
            "tests/test_configuration_reference_claims.py::test_public_optional_watcher_missing_endpoint_exit_and_preview",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "`target` is the configured",
        "`target` is the configured numeric quota used by forecast and quota commands. "
        "`period` selects a calendar half-year (`YYYY-H1` or `YYYY-H2`) or quarter "
        "(`YYYY-Q1` through `YYYY-Q4`).",
        "calendar half-year",
        "calendar month",
        (
            "tests/test_quota_period.py::test_quota_period_end_date_matches_calendar",
            "tests/test_forecast.py::test_forecast_auto_quota_auto_quota_from_config",
            _EXAMPLES + "test_configuration_reference_yaml_examples_are_accepted_by_their_consumers",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "`max_concurrent` controls",
        "`max_concurrent` controls how many eligible, pairwise-disjoint prompt sources a "
        "single `fieldkit driver run` may execute. The supported range is `1` through "
        "`4`; integer values are clamped to that range, and values that cannot be converted "
        "to an integer fall back to `1`. When `FIELDKIT_DRIVER_SPEND_CAP` is set, "
        "the driver limits the batch to one issue so every attempt has an unambiguous "
        "spend boundary.",
        "limits the batch to one issue",
        "permits four issues per batch",
        (
            _EXAMPLES + "test_configuration_reference_driver_bounds",
            "tests/test_driver.py::test_run_driver_limits_capped_execution_to_one_admitted_issue",
            "tests/test_driver_batch.py::test_batch_of_two_disjoint_issues_both_execute",
            "tests/test_driver_batch.py::test_batch_overlapping_second_issue_skipped_with_reason",
        ),
    ),
    ReferenceClaim(
        _CONFIG,
        "Set `pursuit_coverage_threshold`",
        "Set `pursuit_coverage_threshold` on an account to require more active pursuits "
        "in the pipeline review's Pursuit Coverage check. The default is `1`. This "
        "setting is separate from Gmail's `blindspots_min_messages` threshold.",
        "The default is `1`",
        "The default is `20`",
        ("tests/test_configuration_reference_claims.py::test_documented_pursuit_coverage_threshold_is_independent",),
    ),
    ReferenceClaim(
        _CONFIG,
        "Back up the workspace",
        "Back up the workspace and any required runtime state independently. A source "
        "checkout is not a workspace, and reinstalling fieldkit does not delete either "
        "configured data root.",
        "does not delete",
        "deletes",
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Most durable settings",
        "Most durable settings belong in the [fieldkit configuration file](config-file.md), "
        "normally `~/.config/fieldkit/config.yaml`. Environment variables are "
        "appropriate for per-run overrides, CI isolation, and credentials supplied by a "
        "secret manager. Ordinary child processes inherit exported variables. Gmail "
        "authentication also loads the workspace `.env` and attempts dotenv discovery; "
        "existing environment values take precedence over those files.",
        "existing environment values take precedence",
        "dotenv files take precedence",
        (
            "tests/test_dotenv_compat.py::test_load_dotenv_safe_preserves_existing_environment_by_default",
            "tests/test_configuration_reference_claims.py::test_gmail_auth_loads_workspace_then_discovery_without_overriding_environment",
        ),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "The configured domain comes",
        "The configured domain comes from `email_domain`, or from the domain portion of "
        "`email` or `identity.email` when `email_domain` is unset. Without a username or "
        "domain, fieldkit cannot derive an email address from `USER`.",
        "cannot derive",
        "can always derive",
        (_EXAMPLES + "test_environment_reference_identity_resolution",),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "A valid saved Google token",
        "A valid saved Google token, or one that can be refreshed, does not require "
        "duplicate client settings in the environment. Set both client settings when "
        "initial consent or reauthorization is needed, and complete consent in an "
        "interactive terminal.",
        "does not require",
        "always requires",
        ("tests/test_gmail_sync.py::test_cached_token_needs_no_separate_client_settings",),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "The configured model must",
        "The configured model must use a provider route supported by fieldkit. Model "
        "access, billing, data handling, and region availability belong to the provider "
        "and your organization. Any nonempty `FIELDKIT_NO_LLM` value disables provider "
        "calls, including `0`; unset it to enable calls.",
        "including `0`",
        "except `0`",
        (
            _EXAMPLES + "test_environment_reference_no_llm_values",
            _EXAMPLES + "test_environment_reference_no_llm_skips_providers",
        ),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Model selection checks",
        "Model selection checks an explicit caller override, then `FIELDKIT_LLM_MODEL`, "
        "`LLM_MODEL`, `FIELDKIT_ANTHROPIC_MODEL`, and `ANTHROPIC_DEFAULT_SONNET_MODEL`, "
        "before configuration and the default. The Anthropic aliases supply a model name "
        "that fieldkit prefixes with `vertex_ai/`.",
        "before configuration",
        "after configuration",
        (
            _EXAMPLES + "test_environment_reference_model_precedence",
            _EXAMPLES + "test_environment_reference_model_configuration_fallback",
        ),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Region selection checks",
        "Region selection checks configured `vertex_location` first, then the four "
        "environment names in the order shown, and finally `us-east5`. Empty values and "
        "`global` are skipped.",
        "`global` are skipped",
        "`global` are accepted",
        (_EXAMPLES + "test_environment_reference_region_precedence",),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Use absolute paths for",
        "Use absolute paths for predictable behavior across working directories. "
        "`FIELDKIT_DATA_DIR` and `FIELDKIT_HARNESS_ROOT` require absolute roots; "
        "`FIELDKIT_DATA_DIR` may select any absolute runtime-data root. "
        "`FIELDKIT_LLM_LOG` remains restricted to its documented allowed roots.",
        "require absolute roots",
        "accept relative roots",
        (
            _EXAMPLES + "test_environment_reference_absolute_roots",
            _EXAMPLES + "test_environment_reference_llm_log_boundary",
        ),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "If neither `FIELDKIT_HARNESS_ROOT`",
        "If neither `FIELDKIT_HARNESS_ROOT` nor an absolute `XDG_CACHE_HOME` is set, "
        "fieldkit uses `~/.cache/fieldkit` for disposable harness worktrees. This cache is "
        "separate from the application, workspace, and runtime-data roots.",
        "nor an absolute",
        "nor a relative",
        (_EXAMPLES + "test_environment_reference_scratch_cache_fallback",),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "The admission variables govern",
        "The admission variables govern the separately invoked developer-job lease. The "
        "driver spend cap governs a driver iteration itself. A missing required "
        "admission value, invalid value, unreadable spend ledger, or exceeded cap denies "
        "the operation rather than silently running without a limit.",
        "exceeded cap denies",
        "exceeded cap allows",
        (
            "tests/test_driver_admission.py::test_admit_fails_closed_when_run_budget_is_unset",
            "tests/test_driver_admission.py::test_admit_rejects_unreadable_or_exhausted_spend",
            "tests/test_configuration_reference_claims.py::test_developer_admission_rejects_invalid_daily_limit",
            "tests/test_configuration_reference_claims.py::test_developer_admission_rejects_missing_or_invalid_cap",
        ),
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Do not place secrets",
        "Do not place secrets in command-line arguments, shell history, committed dotenv "
        "files, issue bodies, or CI logs. Use the operating system, CI platform, or "
        "organization's approved secret store.",
        "Do not place secrets",
        "Place secrets",
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Set a non-secret value",
        "Set a non-secret value for one invocation:",
        "one invocation",
        "all future shells",
    ),
    ReferenceClaim(
        _ENVIRONMENT,
        "Export a value",
        "Export a value for subsequent commands in the current shell:",
        "current shell",
        "all future shells",
    ),
)


def _assert_claim(document: str, claim: ReferenceClaim) -> None:
    """Match a complete paragraph and refuse duplicate clauses sharing its anchor."""
    paragraphs = [" ".join(part.split()) for part in document.split("\n\n")]
    matching = [paragraph for paragraph in paragraphs if claim.anchor in paragraph]
    assert matching == [claim.paragraph], f"Unreviewed reference paragraph: {claim.anchor}"
    contradiction = claim.paragraph.replace(claim.before, claim.after)
    assert contradiction not in paragraphs, f"Unreviewed reference paragraph: {claim.anchor}"


@pytest.mark.parametrize("claim", _CLAIMS, ids=lambda claim: claim.anchor)
def test_residual_reference_paragraph_is_reviewed(claim: ReferenceClaim) -> None:
    _assert_claim(claim.document.read_text(encoding="utf-8"), claim)


@pytest.mark.parametrize("claim", _CLAIMS, ids=lambda claim: claim.anchor)
@pytest.mark.parametrize("mutation", ["scope", "prefixed-negation", "contradictory-duplicate"])
def test_residual_reference_rejects_semantic_mutation(claim: ReferenceClaim, mutation: str) -> None:
    document = "\n\n".join(" ".join(part.split()) for part in claim.document.read_text(encoding="utf-8").split("\n\n"))
    assert claim.paragraph in document
    assert claim.before in claim.paragraph
    changed = claim.paragraph.replace(claim.before, claim.after)
    if mutation == "prefixed-negation":
        changed = "It is false that " + claim.paragraph
    elif mutation == "contradictory-duplicate":
        changed = claim.paragraph + "\n\n" + changed
    with pytest.raises(AssertionError, match="Unreviewed reference paragraph"):
        _assert_claim(document.replace(claim.paragraph, changed), claim)


@pytest.mark.parametrize("claim", [claim for claim in _CLAIMS if claim.behavior], ids=lambda claim: claim.anchor)
def test_residual_claim_links_resolve_to_canonical_behavior_tests(claim: ReferenceClaim) -> None:
    """Structural routing check only; the fixed owner must execute these nodes."""
    for nodeid in claim.behavior:
        path, function = nodeid.split("::")
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        assert function in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}, nodeid


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ({"pursuit_coverage_threshold": 3, "blindspots_min_messages": 20}, 3),
        ({"blindspot_threshold": 9, "blindspots_min_messages": 20}, 1),
    ],
)
def test_documented_pursuit_coverage_threshold_is_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: dict[str, int],
    expected: int,
) -> None:
    monkeypatch.setattr(
        "fieldkit.pipeline.collect.get_accounts_config", lambda **_kwargs: {"accounts": {"acme": setting}}
    )
    monkeypatch.setattr("fieldkit.pipeline.collect.iterate_pursuits", lambda _root: iter(()))

    coverage = collect_blindspot_data(tmp_path)

    assert len(coverage) == 1
    assert coverage[0]["threshold"] == expected
    assert coverage[0]["status"] == "blindspot"


def test_minimal_initialization_omits_identity_and_defaults_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_api, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.delenv("FIELDKIT_DATA_DIR", raising=False)
    clear_config_caches()
    try:
        result = main(["init", "--minimal", str(workspace)])
        assert result == 0
        values = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert not {"name", "email", "role", "company", "identity", "fieldkit_data"}.intersection(values)
        assert not (workspace / "config" / "identity.yaml").exists()
        assert get_fieldkit_data() == workspace / "data"
    finally:
        clear_config_caches()


def test_workspace_command_missing_home_exits_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()
    try:
        result = main(["pipeline", "quota", "--source", "pursuits"])
        assert result == 3
    finally:
        clear_config_caches()


def test_interactive_setup_retains_workspace_identity_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_api, "CONFIG_PATH", config_path)
    fields = {
        "name": "Example User",
        "email": "user@example.com",
        "role": "Engineer",
        "company": "Example",
        "territory": "Example Territory",
        "salesforce_user_id": "005000000000AAA",
    }
    _wizard_write_artifacts(
        name=fields["name"],
        email=fields["email"],
        role=fields["role"],
        company=fields["company"],
        territory=fields["territory"],
        salesforce_user_id=fields["salesforce_user_id"],
        account_names=[],
        data_dir=workspace,
        oauth_id="",
        oauth_secret="",
    )
    identity = yaml.safe_load((workspace / "config" / "identity.yaml").read_text(encoding="utf-8"))["identity"]
    assert {key: identity[key] for key in fields} == fields
    global_values = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "territory" not in global_values
    assert "salesforce_user_id" not in global_values


def test_optional_integration_table_complete_cells_and_canonical_getters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _CONFIG.read_text(encoding="utf-8").split("## Optional integration keys\n", 1)[1].split("\n## ", 1)[0]
    tables = parse_markdown_tables(body)
    assert len(tables) == 1
    assert [[cell.replace("`", "") for cell in row] for row in tables[0].rows] == [
        [
            "sf_org_url",
            "Salesforce authentication",
            "Base URL for the Salesforce organization you are authorized to access",
        ],
        [
            "gmail_token",
            "Google commands, when overriding the default",
            "Google OAuth token path; relative paths resolve from the current working directory",
        ],
        [
            "gmail_db",
            "Gmail commands, when overriding the default",
            "Local Gmail SQLite cache path; relative paths resolve from the current working directory",
        ],
        ["llm_model", "AI-assisted workflows", "Supported LiteLLM model identifier"],
        ["vertex_location", "Vertex AI workflows", "Provider region"],
        ["mcp_gateway_url", "MCP-backed workflows", "Base URL of a gateway you operate or are authorized to use"],
        [
            "mcp_endpoints.backstory",
            "Backstory health watcher",
            "Full HTTP(S) MCP endpoint supplied by your service operator",
        ],
        [
            "mcp_endpoints.calendar",
            "Calendar section in the assembled morning brief",
            "Full HTTP(S) MCP endpoint supplied by your service operator",
        ],
        [
            "mcp_endpoints.draft_queue",
            "Draft-queue watcher",
            "Full HTTP(S) MCP endpoint supplied by your service operator",
        ],
        [
            "github_repo",
            "GitHub-backed issue and web PR commands",
            "Public or private GitHub repository in owner/repo form",
        ],
        [
            "companion.tier",
            "Companion workflows",
            "read (default), propose, or act; invalid values fail closed to read",
        ],
        [
            "companion.act_allowlist",
            "Companion workflows at act tier",
            "Complete argv entries; empty by default. Every argument, value, and token order must match. Shell-style quotes represent tokens only; no shell is executed.",
        ],
    ]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sf_org_url": "https://example.my.salesforce.com",
                "vertex_location": "europe-west4",
                "llm_model": "vertex_ai/example-model",
                "mcp_gateway_url": "https://gateway.example.com",
                "mcp_endpoints": {"calendar": "https://gateway.example.com/calendar/mcp"},
                "github_repo": "example/project",
                "companion": {"tier": "act", "act_allowlist": ["fieldkit skill list"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()
    try:
        assert get_sf_rest_base_url() == "https://example.my.salesforce.com"
        assert get_vertex_location() == "europe-west4"
        assert get_llm_model() == "vertex_ai/example-model"
        assert get_mcp_gateway_url() == "https://gateway.example.com"
        assert get_mcp_endpoint("calendar") == "https://gateway.example.com/calendar/mcp"
        assert get_mcp_endpoint("backstory") is None
        assert get_mcp_endpoint("draft_queue") is None
        assert get_github_repo() == "example/project"
        assert get_companion_tier() == "act"
        assert get_companion_act_allowlist() == ["fieldkit skill list"]
    finally:
        clear_config_caches()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://10.1.2.3/api",
        "https://[fd00::1]/api",
        "https://[::1]/api",
        "https://assistant.example.com/api#fragment",
        "https://assistant.example.com",
    ],
)
def test_shadowbot_residual_url_cases_reject_before_bearer_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"shadowbot": {"api_base": endpoint}}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    clear_config_caches()
    transport = Mock()
    try:
        with pytest.raises(ConfigError, match="HTTPS"):
            get_shadowbot_api_base()
        with pytest.raises(ConfigError, match="HTTPS"):
            ShadowbotClient("fictional-token")._create_thread(
                transport,
                {"Authorization": "Bearer fictional-token"},
                float("inf"),
                30,
            )
        transport.build_request.assert_not_called()
        transport.send.assert_not_called()
    finally:
        clear_config_caches()


@pytest.mark.parametrize("name", ["backstory", "calendar", "draft_queue"])
@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "gateway.example.com/mcp",
        "ftp://gateway.example.com/mcp",
        "https://user:secret@gateway.example.com/mcp",
        "https://gateway.example.com/mcp?token=secret",
        "https://gateway.example.com/mcp#fragment",
        "https://gateway.example.com:not-a-port/mcp",
    ],
)
def test_all_optional_mcp_getters_omit_or_reject_unsafe_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: config_api.McpEndpointName, endpoint: str | None
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"mcp_endpoints": {name: endpoint}} if endpoint is not None else {}), encoding="utf-8"
    )
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config)
    clear_config_caches()
    try:
        if endpoint is None:
            assert get_mcp_endpoint(name) is None
        else:
            with pytest.raises(ConfigError, match=rf"mcp_endpoints\.{name}"):
                get_mcp_endpoint(name)
    finally:
        clear_config_caches()


@pytest.mark.parametrize(
    "watcher,key,error",
    [
        ("backstory-health", "backstory", "Backstory"),
        ("draft-queue", "draft_queue", "draft-queue"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True], ids=["live", "preview"])
def test_public_optional_watcher_missing_endpoint_exit_and_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    watcher: str,
    key: str,
    error: str,
    dry_run: bool,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "accounts.yaml").write_text(
        "accounts:\n  acme-corp:\n    domains: [acme-corp.example.com]\n    keywords: [Acme Corp]\n", encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"fieldkit_home": str(workspace)}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_api, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main_module, "load_dotenv_safe", Mock(return_value=False))
    provider = Mock(side_effect=AssertionError("provider access is forbidden"))
    monkeypatch.setattr(backstory_health, "MCPSession", provider)
    monkeypatch.setattr(watcher_mcp, "MCPSession", provider)
    clear_config_caches()
    try:
        result = main(["watch", "run", watcher, *(["--dry-run"] if dry_run else [])])
        assert result == (0 if dry_run else 3)
        captured = capsys.readouterr()
        if dry_run:
            assert "Config error" not in captured.err
        else:
            assert captured.err == (
                "[cli_exit] Config error — investigation required: "
                f"Config key 'mcp_endpoints.{key}' is required for the {error} watcher\n"
            )
        provider.assert_not_called()
    finally:
        clear_config_caches()


@pytest.mark.parametrize("limit", ["invalid", "0", "-1", "1.5", ""])
def test_developer_admission_rejects_invalid_daily_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(admission, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.setenv("FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT", limit)
    monkeypatch.setenv("FIELDKIT_DEVELOPER_SPEND_CAP", "5")
    decision = admission.admit("driver")
    assert decision.allowed is False
    assert decision.reason_code == "daily-run-limit-unset"
    ledger = json.loads((tmp_path / "driver" / "developer-admission.json").read_text(encoding="utf-8"))
    assert "active" not in ledger
    assert "runs" not in ledger


@pytest.mark.parametrize("cap", [None, "", "invalid", "-1", "nan", "inf"])
def test_developer_admission_rejects_missing_or_invalid_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: str | None
) -> None:
    monkeypatch.setattr(admission, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.setenv("FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT", "1")
    monkeypatch.delenv("FIELDKIT_DEVELOPER_SPEND_CAP", raising=False)
    if cap is not None:
        monkeypatch.setenv("FIELDKIT_DEVELOPER_SPEND_CAP", cap)
    ledger_read = Mock(return_value=0.0)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", ledger_read)
    decision = admission.admit("driver")
    assert decision.allowed is False
    assert decision.reason_code == ("spend-cap-unset" if cap is None else "spend-cap-invalid")
    ledger_read.assert_not_called()


@pytest.mark.parametrize("workspace_available", [True, False])
@pytest.mark.parametrize("existing", [True, False])
def test_gmail_auth_loads_workspace_then_discovery_without_overriding_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_available: bool, existing: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "GOOGLE_OAUTH_CLIENT_ID=workspace-id\nGOOGLE_OAUTH_CLIENT_SECRET=workspace-secret\n", encoding="utf-8"
    )
    discovery = tmp_path / ".env"
    discovery.write_text(
        "GOOGLE_OAUTH_CLIENT_ID=discovery-id\nGOOGLE_OAUTH_CLIENT_SECRET=discovery-secret\n", encoding="utf-8"
    )
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    if existing:
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "environment-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "environment-secret")
    home = Mock(
        return_value=workspace, side_effect=None if workspace_available else ConfigError("workspace unavailable")
    )
    monkeypatch.setattr(gmail_auth, "get_fieldkit_home", home)
    monkeypatch.setattr(gmail_auth, "_get_token_path", lambda: tmp_path / "absent-token.json")
    find = Mock(return_value=str(discovery))
    monkeypatch.setattr(dotenv.main, "find_dotenv", find)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    flow_factory = Mock(side_effect=RuntimeError("isolated consent boundary"))
    monkeypatch.setattr(InstalledAppFlow, "from_client_config", flow_factory)
    gmail_auth._data_dir.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="isolated consent boundary"):
            gmail_auth.get_gmail_service()
        expected = "environment" if existing else "workspace" if workspace_available else "discovery"
        supplied = flow_factory.call_args.args[0]["installed"]
        assert supplied["client_id"] == f"{expected}-id"
        assert supplied["client_secret"] == f"{expected}-secret"
        assert os.environ["GOOGLE_OAUTH_CLIENT_ID"] == f"{expected}-id"
        find.assert_called_once_with()
    finally:
        gmail_auth._data_dir.cache_clear()


# Ordered structural review only: these literals do not prove provider access,
# administrator approval, shell behavior, backup/reinstall safety, or secret policy.
_REFERENCE_INVENTORY = {
    _CONFIG: (
        "--- last_reviewed: 2026-09-29 covers: - src/fieldkit/config/ - src/fieldkit/commands/init/ - src/fieldkit/commands/brief/ - src/fieldkit/commands/pipeline/ - src/fieldkit/pipeline/ - src/fieldkit/driver/ - src/fieldkit/companion/ - src/fieldkit/shadowbot/ - src/fieldkit/sf/quota.py audience: user ---",
        "# Configuration file",
        "fieldkit's user configuration is YAML at:",
        "```text ~/.config/fieldkit/config.yaml ```",
        "When `XDG_CONFIG_HOME` is an absolute path, fieldkit instead uses `$XDG_CONFIG_HOME/fieldkit/config.yaml`. The Salesforce cookie file follows the same directory. Relative XDG paths are ignored, as required by the XDG base directory specification.",
        "Prefer `fieldkit init` to create or update it. Paths must be absolute where the table says so. Keep credentials out of source control and use the provider's documented token location or environment variable instead of inventing new keys.",
        "## Core keys",
        "| Key | Required | Default | Purpose | | --- | --- | --- | --- | | `fieldkit_home` | Yes for a configured workspace | None | Absolute path to user-owned workspace files | | `fieldkit_data` | No | `<fieldkit_home>/data` | Absolute path to runtime databases, tokens, logs, and generated state | | `name` | No | None | Display name used by identity-aware local workflows | | `email` | No | None | User email used to exclude self-authored records | | `email_domain` | No | None | Organization domain used only when a command must derive the user email | | `role` | No | None | User-provided role retained by interactive setup | | `company` | No | None | User-provided organization retained by interactive setup |",
        "Minimal initialization creates a generic workspace without identity values:",
        "```console fieldkit init --minimal ./fieldkit-workspace ```",
        "## Optional integration keys",
        "| Key | Required by | Purpose | | --- | --- | --- | | `sf_org_url` | Salesforce authentication | Base URL for the Salesforce organization you are authorized to access | | `gmail_token` | Google commands, when overriding the default | Google OAuth token path; relative paths resolve from the current working directory | | `gmail_db` | Gmail commands, when overriding the default | Local Gmail SQLite cache path; relative paths resolve from the current working directory | | `llm_model` | AI-assisted workflows | Supported LiteLLM model identifier | | `vertex_location` | Vertex AI workflows | Provider region | | `mcp_gateway_url` | MCP-backed workflows | Base URL of a gateway you operate or are authorized to use | | `mcp_endpoints.backstory` | Backstory health watcher | Full HTTP(S) MCP endpoint supplied by your service operator | | `mcp_endpoints.calendar` | Calendar section in the assembled morning brief | Full HTTP(S) MCP endpoint supplied by your service operator | | `mcp_endpoints.draft_queue` | Draft-queue watcher | Full HTTP(S) MCP endpoint supplied by your service operator | | `github_repo` | GitHub-backed issue and web PR commands | Public or private GitHub repository in `owner/repo` form | | `companion.tier` | Companion workflows | `read` (default), `propose`, or `act`; invalid values fail closed to `read` | | `companion.act_allowlist` | Companion workflows at `act` tier | Complete argv entries; empty by default. Every argument, value, and token order must match. Shell-style quotes represent tokens only; no shell is executed. |",
        "Workspace identity written by interactive setup lives separately at `<fieldkit_home>/config/identity.yaml`. Its `territory` and `salesforce_user_id` values are template metadata, not global integration settings. Setup also records the supplied name, email, role, and company there for workspace-owned templates.",
        "Companion permission is not operator authorization or an operating-system sandbox. `companion allowed` checks the configured gate without executing the candidate command. `companion run` executes permitted commands and writes an audit journal, including denied attempts. Default `companion feed` polling can write a delivery cursor; `companion feed --json --all` avoids advancing it. The gate admits feed inspection through its read-tier table only when `--all` is an actual option, not an account value. Unknown feed options fail closed. Quota reporting through the read-tier table rejects `--set`, `--period`, `--data-root`, and unknown options. Selecting `--source sf` still performs credentialed Salesforce reads; read permission does not mean offline execution. Saved report selection requires `brief open --no-open` or `pipeline open --no-open`; `--json` alone still launches a system viewer and is not granted read-tier permission. Unknown report-selection options fail closed. Approve the actual account/source scope and local or remote effects separately; do not treat the `read` tier as a universal no-filesystem-write guarantee.",
        "`fieldkit_home` is the required workspace-root key. If it is absent, commands that need the workspace fail with exit `3`; run `fieldkit init` or `fieldkit init --minimal PATH` to write a current configuration.",
        "Installing an optional profile does not populate any of these keys or grant service access. See [Integrations and profiles](../integrations.md).",
        "The three `mcp_endpoints` values are independent, optional capabilities. fieldkit does not derive private route names or append organization-specific paths. Supply the complete endpoint exactly as provided by an operator you trust:",
        "```yaml mcp_endpoints: calendar: https://gateway.example.com/calendar/mcp ```",
        "Endpoints must use HTTP or HTTPS and cannot contain embedded credentials, query parameters, or fragments. Leave a capability absent when you do not have that service; local aggregate workflows report it as not run. Explicitly selecting a live `backstory-health` or `draft-queue` watcher without its required endpoint is invalid configuration and exits `3`. Their dry runs do not require provider access.",
        "## Pipeline quota",
        '```yaml pipeline: quota: target: 5000000 period: "2026-H2" ```',
        "`target` is the configured numeric quota used by forecast and quota commands. `period` selects a calendar half-year (`YYYY-H1` or `YYYY-H2`) or quarter (`YYYY-Q1` through `YYYY-Q4`).",
        "## Driver scheduling",
        "```yaml driver: max_concurrent: 1 ```",
        "`max_concurrent` controls how many eligible, pairwise-disjoint prompt sources a single `fieldkit driver run` may execute. The supported range is `1` through `4`; integer values are clamped to that range, and values that cannot be converted to an integer fall back to `1`. When `FIELDKIT_DRIVER_SPEND_CAP` is set, the driver limits the batch to one issue so every attempt has an unambiguous spend boundary.",
        "The driver accepts `WorkOrder: docs/work-orders/<name>.md`, `OpenSpec: openspec/changes/<name>/`, and `Speckit: specs/<name>/` issue references. A work order carries its versioned `edit_sites` and `done_checks` contracts in frontmatter. OpenSpec and Speckit directories use `driver.yaml` for both. The driver validates every exact anchor against the frozen `origin/main` revision before scheduling and creates the execution worktree from that same commit. Missing or ambiguous anchors remain non-passing; prose fences and line numbers are never inferred as authority.",
        "## Organization-provided assistant",
        "The optional `shadowbot` adapter uses a nested section whose URLs and identifiers must come from the service administrator:",
        "```yaml shadowbot: api_base: https://assistant.example.com/api token_endpoint: https://login.example.com/oauth/token auth_endpoint: https://login.example.com/oauth/authorize redirect_uri: https://assistant.example.com/oauth/callback client_id: example-client assistant_id: example-assistant ```",
        "The open-source project does not operate this service or provide working values. Do not copy sample endpoints into a real deployment.",
        "`api_base` must be a public HTTPS URL with a path. fieldkit rejects local and private literal addresses, non-default HTTPS ports, embedded credentials, query parameters, and fragments before it sends an authorization bearer token.",
        "## Account configuration",
        "Account-specific settings live at `<fieldkit_home>/config/accounts.yaml`, separate from global configuration:",
        "```yaml internal_domains: - example.com gmail_label_prefix: ref/ accounts: acme-corp: domains: - acme-corp.example.com keywords: - Acme Corp ```",
        "The `accounts` mapping is keyed by a filesystem-safe account slug. Common optional fields include Salesforce record or territory identifiers, Gmail search keywords, team addresses, and watcher thresholds. Start with the file generated by `fieldkit init`; use the relevant command's `--help` before adding an advanced field.",
        "Set `pursuit_coverage_threshold` on an account to require more active pursuits in the pipeline review's Pursuit Coverage check. The default is `1`. This setting is separate from Gmail's `blindspots_min_messages` threshold.",
        "An absent `accounts.yaml` means no optional account overrides. When a transcript has action items to classify, task preparation requires a readable, valid mapping if the file is present: malformed YAML, an invalid selected account entry, or invalid attendee lists stop preparation rather than silently changing task ownership. Brief and pipeline reports read `config/accounts.yaml` in their selected workspace, including an explicit `--data-root`, without falling back to a different workspace. When these reports consult account configuration, invalid or duplicate content stops generation with an invalid-data error. Other optional configuration consumers may warn and use empty overrides; that fallback does not apply to task preparation or these report reads.",
        "## Three-root boundary",
        "Workspace and runtime-data roots must be absolute after `~` expansion. The optional `fieldkit_root` override selects an application checkout and has the same requirement; empty or invalid overrides are configuration errors. Skill discovery uses this validated override when present and bundled package resources otherwise. Relative paths passed to initialization are resolved before being stored in configuration.",
        "| Content | Resolver | Ownership | | --- | --- | --- | | Bundled package assets | `importlib.resources` | Application; available without a checkout | | Explicit or discovered source checkout | `get_fieldkit_root()` | Application source; unavailable in a checkout-free install without an override | | Workspace | `get_fieldkit_home()` | User-authored and optionally versioned data | | Runtime data | `get_fieldkit_data()` | Application-managed caches, credentials, logs, and state |",
        "Back up the workspace and any required runtime state independently. A source checkout is not a workspace, and reinstalling fieldkit does not delete either configured data root.",
        "## Validate a change",
        "```console fieldkit doctor ```",
        "The general doctor checks Salesforce, the Gmail cache, Google OAuth, and ShadowBot. Invalid or incomplete data detected by those checks exits `3`; an authentication problem exits `2`. Changing unrelated configuration will not fix an authentication failure.",
        "A passing result does not validate every LLM, MCP, or driver setting. Follow the affected workflow's documented diagnostics or non-writing preview before enabling its writes; do not treat doctor as a universal configuration validator.",
    ),
    _ENVIRONMENT: (
        "--- last_reviewed: 2026-09-28 covers: - src/fieldkit/config/ - src/fieldkit/llm/ - src/fieldkit/commands/gmail/ audience: user ---",
        "# Environment variables",
        "Most durable settings belong in the [fieldkit configuration file](config-file.md), normally `~/.config/fieldkit/config.yaml`. Environment variables are appropriate for per-run overrides, CI isolation, and credentials supplied by a secret manager. Ordinary child processes inherit exported variables. Gmail authentication also loads the workspace `.env` and attempts dotenv discovery; existing environment values take precedence over those files.",
        "## User identity",
        "| Variable | Purpose | | --- | --- | | `FIELDKIT_USER_EMAIL` | Explicit current-user email for email-derived workflows | | `USER` | Shell username used with the configured email domain when FIELDKIT_USER_EMAIL is empty |",
        "Prefer `FIELDKIT_USER_EMAIL` when email-derived workflows are enabled. Use a fictional address in tests and examples.",
        "The configured domain comes from `email_domain`, or from the domain portion of `email` or `identity.email` when `email_domain` is unset. Without a username or domain, fieldkit cannot derive an email address from `USER`.",
        "## Google OAuth",
        "| Variable | Fallback alias | Purpose | | --- | --- | --- | | `GOOGLE_OAUTH_CLIENT_ID` | None | OAuth client identifier for Google consent | | `GOOGLE_OAUTH_CLIENT_SECRET` | None | OAuth client secret |",
        "A valid saved Google token, or one that can be refreshed, does not require duplicate client settings in the environment. Set both client settings when initial consent or reauthorization is needed, and complete consent in an interactive terminal.",
        "## AI and transcription",
        "| Variable | Fallback alias | Purpose | | --- | --- | --- | | `FIELDKIT_LLM_MODEL` | `LLM_MODEL` | Supported LiteLLM model override | | `FIELDKIT_ANTHROPIC_MODEL` | `ANTHROPIC_DEFAULT_SONNET_MODEL` | Vertex AI model name fallback after the general model environment variables | | `FIELDKIT_NO_LLM` | None | Disable provider calls and select documented deterministic no-AI behavior | | `FIELDKIT_TRANSCRIBE_MODEL` | None | Explicit transcription model; no provider is selected by default | | `FIELDKIT_VERTEX_LOCATION` | `CLOUD_ML_REGION`, `VERTEX_LOCATION`, `GOOGLE_CLOUD_REGION` | Vertex AI region fallback after configured `vertex_location` | | `VERTEXAI_PROJECT` | None | Explicit Vertex AI project read by LiteLLM | | `GOOGLE_CLOUD_PROJECT` | None | Project used by Google application-default credential discovery when no explicit LiteLLM project is selected | | `GOOGLE_APPLICATION_CREDENTIALS` | None | Standard Google credential-file path used by provider tooling |",
        "The configured model must use a provider route supported by fieldkit. Model access, billing, data handling, and region availability belong to the provider and your organization. Any nonempty `FIELDKIT_NO_LLM` value disables provider calls, including `0`; unset it to enable calls.",
        "Model selection checks an explicit caller override, then `FIELDKIT_LLM_MODEL`, `LLM_MODEL`, `FIELDKIT_ANTHROPIC_MODEL`, and `ANTHROPIC_DEFAULT_SONNET_MODEL`, before configuration and the default. The Anthropic aliases supply a model name that fieldkit prefixes with `vertex_ai/`.",
        "Region selection checks configured `vertex_location` first, then the four environment names in the order shown, and finally `us-east5`. Empty values and `global` are skipped.",
        "## Path overrides",
        "| Variable | Purpose | | --- | --- | | `XDG_CONFIG_HOME` | Absolute base directory for fieldkit configuration and Salesforce cookie files; useful for isolated trials and CI | | `XDG_CACHE_HOME` | Absolute cache base; the harness scratch root defaults to its `fieldkit/` child | | `FIELDKIT_DATA_DIR` | Absolute runtime-data root override | | `FIELDKIT_HARNESS_ROOT` | Absolute scratch root for disposable harness worktrees; overrides `XDG_CACHE_HOME` | | `FIELDKIT_LLM_LOG` | Absolute LLM-call database path within an allowed fieldkit root | | `FIELDKIT_SKILLS_DIR` | Skill directory override; relative paths resolve against the current working directory | | `FIELDKIT_MCP_GATEWAY_URL` | MCP gateway base URL fallback when `mcp_gateway_url` is not configured |",
        "Use absolute paths for predictable behavior across working directories. `FIELDKIT_DATA_DIR` and `FIELDKIT_HARNESS_ROOT` require absolute roots; `FIELDKIT_DATA_DIR` may select any absolute runtime-data root. `FIELDKIT_LLM_LOG` remains restricted to its documented allowed roots.",
        "The LLM log override must resolve beneath `~/.config/fieldkit`, `~/.local/share/fieldkit`, the workspace, the active runtime-data root, or the configured runtime-data root, when those configured roots are available. Changing `XDG_CONFIG_HOME` does not by itself approve that directory as an LLM log root. These checks resolve paths before comparing them with the allowed roots.",
        "If neither `FIELDKIT_HARNESS_ROOT` nor an absolute `XDG_CACHE_HOME` is set, fieldkit uses `~/.cache/fieldkit` for disposable harness worktrees. This cache is separate from the application, workspace, and runtime-data roots.",
        "## Driver limits",
        "| Variable | Purpose | | --- | --- | | `FIELDKIT_DRIVER_SPEND_CAP` | Optional non-negative USD daily cap for driver LLM calls; invalid, unreadable, or reached caps deny the run, and any configured cap limits an admitted non-dry batch to one issue | | `FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT` | Required positive daily run count for `fieldkit driver admit` | | `FIELDKIT_DEVELOPER_SPEND_CAP` | Required non-negative USD cap for `fieldkit driver admit` |",
        "The admission variables govern the separately invoked developer-job lease. The driver spend cap governs a driver iteration itself. A missing required admission value, invalid value, unreadable spend ledger, or exceeded cap denies the operation rather than silently running without a limit.",
        "## One command or one shell",
        "Set a non-secret value for one invocation:",
        "```console FIELDKIT_NO_LLM=1 fieldkit brief generate --pipeline-only --dry-run ```",
        "Export a value for subsequent commands in the current shell:",
        "```console export FIELDKIT_USER_EMAIL=user@example.com fieldkit doctor ```",
        "Do not place secrets in command-line arguments, shell history, committed dotenv files, issue bodies, or CI logs. Use the operating system, CI platform, or organization's approved secret store.",
        "## Organization-provided adapters",
        "Some optional adapters require additional variables or browser-session material defined by the service operator. Those services are not part of fieldkit's portable-core guarantee. Follow the administrator's private deployment guidance and never publish session tokens while asking the fieldkit community for help.",
    ),
}


def _assert_complete_reference(document: str, reference: Path) -> None:
    blocks = tuple(" ".join(part.split()) for part in document.strip().split("\n\n"))
    assert blocks == _REFERENCE_INVENTORY[reference], "Unreviewed complete reference inventory"


@pytest.mark.parametrize("document", [_CONFIG, _ENVIRONMENT])
def test_complete_reference_inventory_is_reviewed(document: Path) -> None:
    _assert_complete_reference(document.read_text(encoding="utf-8"), document)


@pytest.mark.parametrize("document", [_CONFIG, _ENVIRONMENT])
def test_complete_reference_rejects_unanchored_false_paragraph(document: Path) -> None:
    original = document.read_text(encoding="utf-8")
    changed = original + "\nEvery command automatically authorizes all remote writes.\n"
    with pytest.raises(AssertionError):
        _assert_complete_reference(changed, document)


@pytest.mark.parametrize(
    "document,index",
    [(document, index) for document, blocks in _REFERENCE_INVENTORY.items() for index in range(len(blocks))],
    ids=[
        f"{document.stem}-block-{index}"
        for document, blocks in _REFERENCE_INVENTORY.items()
        for index in range(len(blocks))
    ],
)
@pytest.mark.parametrize("mutation", ["delete", "insert", "duplicate", "reorder", "negation", "content"])
def test_complete_reference_rejects_changes_to_every_reviewed_block(document: Path, index: int, mutation: str) -> None:
    blocks = list(_REFERENCE_INVENTORY[document])
    if mutation == "delete":
        del blocks[index]
    elif mutation == "insert":
        blocks.insert(index, "Unreviewed automatic remote authorization.")
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    elif mutation == "reorder":
        neighbor = (index + 1) % len(blocks)
        blocks[index], blocks[neighbor] = blocks[neighbor], blocks[index]
    elif mutation == "negation":
        blocks[index] = "It is false that " + blocks[index]
    else:
        blocks[index] = blocks[index].replace("fieldkit", "unreviewed-product", 1) + " changed"
    with pytest.raises(AssertionError, match="Unreviewed complete reference inventory"):
        _assert_complete_reference("\n\n".join(blocks), document)
