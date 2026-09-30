"""Reviewed first-user pages and actual isolated portable-core trial evidence."""

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
import yaml

from tests.documentation_workflow_support import snapshot_workflow

pytestmark = pytest.mark.integration
CHILD_TIMEOUT_SECONDS = 30
REPO = Path(__file__).resolve().parents[1]
FIRST_USER_GUIDE_NODES = (
    "tests/test_first_user_guides_contract.py",
    "tests/test_init_guide_contract.py::test_real_minimal_init_and_preserving_rerun",
    "tests/test_init_guide_contract.py::test_real_minimal_workspace_discovers_bundled_workflows",
    "tests/test_init_guide_contract.py::test_real_invalid_answers_leave_every_file_unchanged",
    "tests/test_installation_profiles.py::test_base_profile_supports_offline_first_success",
    "tests/test_installation_profiles.py::test_base_doctor_accepts_unconfigured_integrations",
    "tests/test_installation_profiles.py::test_base_doctor_preserves_auth_failure_for_enabled_integration",
    "tests/test_installation_profiles.py::test_base_doctor_reports_missing_explicit_google_token_as_auth_required",
    "tests/test_installation_profiles.py::test_base_doctor_rejects_incomplete_shadowbot_configuration",
    "tests/test_installation_profiles.py::test_profile_success_surface",
    "tests/test_installation_profiles.py::test_base_profile_reports_actionable_missing_profile",
    "tests/test_doctor.py::test_doctor_bare_all_healthy_exits_zero",
    "tests/test_doctor.py::test_aggregate_doctor_preserves_failure_priority",
    "tests/test_smoke_artifact.py::test_first_use_doctor_requires_all_services_disabled_and_clean_stderr",
    "tests/test_smoke_artifact.py::test_first_use_skill_list_requires_known_packaged_skills_and_clean_stderr",
    "tests/test_smoke_artifact.py::test_getting_started_trial_is_ordered_isolated_and_resolves_trial_roots",
    "tests/test_smoke_artifact.py::test_trial_recipe_binding_rejects_changes",
    "tests/test_smoke_artifact.py::test_isolated_trial_clears_external_skill_selector_in_recipe_and_runner",
    "tests/test_smoke_artifact.py::test_published_first_use_sequences_are_ordered_and_share_only_their_own_roots",
    "tests/test_smoke_artifact.py::test_smoke_records_exact_digest_environment_and_all_contracts",
    "tests/test_smoke_artifact.py::test_all_profile_installs_exact_artifact_extra_and_exercises_each_integration",
    "tests/test_smoke_artifact.py::test_uv_installer_targets_the_exact_artifact_and_created_environment",
    "tests/test_check_documentation_example_scenarios.py::test_check_binds_each_safe_documentation_block_to_fixed_smoke_criteria",
    "tests/test_check_documentation_example_scenarios.py::test_check_fails_when_a_documented_command_criterion_is_missing",
    "tests/test_check_documentation_example_scenarios.py::test_check_rejects_failed_artifact_even_when_mapped_examples_pass",
    "tests/test_pursuit_create.py::test_create_cmd_creates_pursuit_file",
    "tests/test_pursuit_create.py::test_create_cmd_json_dry_run_does_not_write",
    "tests/test_pipeline_workflow_guide_contract.py::test_real_audit_json_has_unavailable_qualification_and_no_writes",
    "tests/test_pipeline_workflow_guide_contract.py::test_real_advance_pending_then_explicit_override_preserves_historical_data",
    "tests/test_pipeline_workflow_guide_contract.py::test_real_forecast_scopes_account_preserves_acv_precedence_and_overrides_quota",
    "tests/test_optional_integration_plan.py::test_base_plan_selects_only_local_work",
    "tests/test_brief_documentation_scenarios.py::test_documented_morning_brief_previews_are_offline_and_read_only",
    "tests/test_pipeline_sf_sync_documentation.py::test_documented_listview_json_format_does_not_choose_write_authority",
    "tests/test_gmail_published_sync.py::test_complete_provider_page_publishes_rows_and_checkpoint_together",
    "tests/test_documentation_integration_profiles.py::test_integration_profile_table_matches_installation_and_import_contract",
    "tests/test_documentation_integration_profiles.py::test_google_library_installation_does_not_supply_oauth_credentials",
    "tests/test_check_dependency_profiles.py::test_live_dependency_profile_contract_passes",
    "tests/test_check_compatibility_policy.py::test_live_compatibility_contract_passes",
    "tests/test_quality_contract.py::test_public_contributor_targets_use_locked_environment_and_canonical_gate",
    "tests/test_pursuit_create.py::test_create_cmd_exits_nonzero_missing_account_dir",
    "tests/test_watcher_guide_contract.py::test_real_unconfigured_aggregate_runs_four_locals_and_nonempty_no_llm_brief",
    "tests/test_pipeline_workflow_guide_contract.py::test_real_pipeline_generation_and_open_use_the_same_scope_without_a_viewer",
    "tests/test_documentation_pursuit_workflow.py::test_documentation_audit_output_and_default_report",
)


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    page: str
    line: int
    classification: str
    evidence: str
    text: str


# Every source block is retained literally, including metadata and policy markers.
INVENTORY = (
    GuideClaim("readme.b01", "README.md", 1, "structure", "layout", "# fieldkit"),
    GuideClaim(
        "readme.b02",
        "README.md",
        3,
        "structure",
        "layout",
        "[![CI](https://github.com/mpeter/fieldkit-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/mpeter/fieldkit-cli/actions/workflows/ci.yml)\n[![Artifact compatibility](https://github.com/mpeter/fieldkit-cli/actions/workflows/compatibility.yml/badge.svg)](https://github.com/mpeter/fieldkit-cli/actions/workflows/compatibility.yml)\n[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)\n[![Python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.14-blue.svg)](docs/compatibility.md)",
    ),
    GuideClaim(
        "readme.b03",
        "README.md",
        8,
        "policy",
        "product",
        "fieldkit is a local-first command-line workspace for structured sales and account\nwork. It combines Markdown-based pursuits and tasks with optional Salesforce,\nGoogle, AI, web, and organization-provided integrations. Your workspace remains\nseparate from the application and under your control.",
    ),
    GuideClaim(
        "readme.b04",
        "README.md",
        13,
        "policy",
        "publication",
        "A source version is not a published release. Publication is established only by\nan immutable release record with a signed tag, GitHub release, and package\nartifacts; see [Release history](docs/releases.md) to verify published versions.\nFuture product work is tracked in the [roadmap](ROADMAP.md).",
    ),
    GuideClaim(
        "readme.b05", "README.md", 18, "structure", "layout", "## Quickstart: install and prove the portable core"
    ),
    GuideClaim(
        "readme.b06",
        "README.md",
        20,
        "policy",
        "prerequisites",
        "You need a [supported Python version](docs/compatibility.md) and\n[`uv`](https://docs.astral.sh/uv/getting-started/installation/).",
    ),
    GuideClaim(
        "readme.b07", "README.md", 23, "manual", "manual-install", "```console\nuv tool install fieldkit-cli\n```"
    ),
    GuideClaim(
        "readme.b08",
        "README.md",
        27,
        "behavior",
        "trial",
        "After installation, create and verify a credential-free local workspace:",
    ),
    GuideClaim(
        "readme.b09",
        "README.md",
        29,
        "behavior",
        "trial",
        "```console\nfieldkit init --minimal ./fieldkit-workspace\nfieldkit doctor\nfieldkit skill list\n```",
    ),
    GuideClaim(
        "readme.b10",
        "README.md",
        35,
        "behavior",
        "minimal",
        "`init --minimal` creates a generic local workspace and makes it active in your\nfieldkit user configuration. With a fresh configuration and no integrations\nenabled, `doctor` exits successfully while reporting each unconfigured\nintegration as disabled. With no `FIELDKIT_SKILLS_DIR` or configured\n`fieldkit_root` override, `skill list` prints the workflows bundled with the\ninstalled package. This first success needs no service credentials.",
    ),
    GuideClaim(
        "readme.b11",
        "README.md",
        42,
        "behavior",
        "trial",
        "The command updates your fieldkit user configuration. If you already use\nfieldkit, follow the isolated trial in [Installation and first\nsuccess](docs/getting-started.md) instead.",
    ),
    GuideClaim("readme.b12", "README.md", 46, "structure", "layout", "## What it can do"),
    GuideClaim(
        "readme.b13",
        "README.md",
        48,
        "behavior",
        "capabilities",
        "- Create and validate local pursuit workspaces.\n- Audit, forecast, and advance structured opportunity notes.\n- Run local watchers and assemble a morning brief.\n- Add Salesforce and Gmail context when you configure those services.\n- Enable AI-assisted, local web, browser-auth, or organization-provided\n  capabilities independently.\n- Expose stable text, Markdown, and JSON output for scripts and agents.",
    ),
    GuideClaim(
        "readme.b14",
        "README.md",
        56,
        "navigation",
        "navigation",
        "Start with [What fieldkit does](docs/user-guide.md), then choose integrations from\n[Integrations and profiles](docs/integrations.md). Read\n[Local data and privacy](docs/privacy.md) before using real account or customer\ninformation.",
    ),
    GuideClaim("readme.b15", "README.md", 61, "structure", "layout", "## Choose a capability profile"),
    GuideClaim(
        "readme.b16",
        "README.md",
        63,
        "policy",
        "profiles",
        "The base package is intentionally small. You can select a profile at install\ntime instead:",
    ),
    GuideClaim(
        "readme.b17",
        "README.md",
        66,
        "manual",
        "manual-profile",
        "```console\nuv tool install 'fieldkit-cli[google]'\nuv tool install 'fieldkit-cli[llm]'\nuv tool install 'fieldkit-cli[web]'\nuv tool install 'fieldkit-cli[chrome-auth]'\nuv tool install 'fieldkit-cli[all]'\n```",
    ),
    GuideClaim(
        "readme.b18",
        "README.md",
        74,
        "policy",
        "profile-authorization",
        "Installing a profile downloads its Python dependencies; it does not configure a\nprovider, grant access, or transmit your fieldkit workspace data. If the base\ntool is already installed, add `--force` to replace its tool environment with\nthe selected profile. See [Integrations and profiles](docs/integrations.md)\nbefore enabling a service.",
    ),
    GuideClaim("readme.b19", "README.md", 80, "structure", "layout", "## Contribute"),
    GuideClaim(
        "readme.b20",
        "README.md",
        82,
        "policy",
        "contributor",
        "fieldkit welcomes bug reports, documentation, tests, code, design discussion,\nand issue triage. A contributor checkout has one supported setup path. Before\nrunning `pr-check`, prepare a clean committed candidate and set `QUALITY_BASE`\nto the reviewed full commit SHA as described in [CONTRIBUTING.md](CONTRIBUTING.md):",
    ),
    GuideClaim(
        "readme.b21",
        "README.md",
        87,
        "manual",
        "manual-contributor",
        "```console\ngit clone https://github.com/<your-user>/fieldkit-cli.git\ncd fieldkit-cli\nmake bootstrap\nmake pr-check\n```",
    ),
    GuideClaim(
        "readme.b22",
        "README.md",
        94,
        "navigation",
        "navigation",
        "Read [CONTRIBUTING.md](CONTRIBUTING.md) before starting. Project authority is\ndescribed in [GOVERNANCE.md](GOVERNANCE.md), support routes in\n[SUPPORT.md](SUPPORT.md), and private vulnerability reporting in\n[SECURITY.md](SECURITY.md).",
    ),
    GuideClaim(
        "readme.b23",
        "README.md",
        99,
        "navigation",
        "navigation",
        "Maintainers preparing a release should follow [RELEASING.md](RELEASING.md).",
    ),
    GuideClaim(
        "readme.b24",
        "README.md",
        101,
        "policy",
        "dependency-policy",
        "Dependency changes follow the [dependency security policy](docs/dependency-security.md), including\nlicense review, locked-graph auditing, and narrow exception requirements.",
    ),
    GuideClaim("readme.b25", "README.md", 104, "structure", "layout", "## Compatibility and status"),
    GuideClaim(
        "readme.b26",
        "README.md",
        106,
        "policy",
        "compatibility",
        "The portable core is tested on CPython 3.11 through 3.14 on Ubuntu and macOS,\nwith a Fedora/RHEL-family container smoke path. Native Windows is experimental.\nIndividual integrations can have narrower platform or access requirements; see\n[Compatibility](docs/compatibility.md).",
    ),
    GuideClaim(
        "readme.b27",
        "README.md",
        111,
        "policy",
        "compatibility-policy",
        "The public compatibility contract for 1.x and the distinction between supported\nand internal interfaces are documented in [Product model](docs/concepts.md).",
    ),
    GuideClaim("readme.b28", "README.md", 114, "structure", "layout", "## License"),
    GuideClaim(
        "readme.b29",
        "README.md",
        116,
        "navigation",
        "license",
        "fieldkit is licensed under the [Apache License 2.0](LICENSE).",
    ),
    GuideClaim(
        "getting-started.b01",
        "docs/getting-started.md",
        1,
        "structure",
        "metadata",
        "---\nlast_reviewed: 2026-09-29\ncovers:\n  - pyproject.toml\n  - src/fieldkit/__main__.py\n  - src/fieldkit/commands/init/\n  - src/fieldkit/commands/doctor/\n  - src/fieldkit/config/\n  - src/fieldkit/commands/skill/\n  - src/fieldkit/skill/\n  - src/fieldkit/skills/\naudience: user\n---",
    ),
    GuideClaim(
        "getting-started.b02", "docs/getting-started.md", 15, "structure", "layout", "# Installation and first success"
    ),
    GuideClaim(
        "getting-started.b03",
        "docs/getting-started.md",
        17,
        "behavior",
        "trial",
        "This guide proves the portable fieldkit core before you connect an external\nservice. It does not require Salesforce, Google, an AI provider, or access to an\norganization-specific system.",
    ),
    GuideClaim("getting-started.b04", "docs/getting-started.md", 21, "structure", "layout", "## Prerequisites"),
    GuideClaim(
        "getting-started.b05",
        "docs/getting-started.md",
        23,
        "policy",
        "prerequisites",
        "- Python 3.11 or newer on a [supported platform](compatibility.md).\n- [`uv`](https://docs.astral.sh/uv/getting-started/installation/).",
    ),
    GuideClaim(
        "getting-started.b06",
        "docs/getting-started.md",
        26,
        "policy",
        "compatibility-policy",
        "<!-- compatibility-policy:start -->",
    ),
    GuideClaim(
        "getting-started.b07",
        "docs/getting-started.md",
        27,
        "policy",
        "compatibility",
        "fieldkit's portable core is tested on CPython 3.11, CPython 3.12, CPython 3.13, and CPython 3.14 across `ubuntu-24.04` and `macos-15`. The same core contract runs with Fedora 43's system Python in a `fedora:43` container. CI separately installs the exact candidate with its `all` optional profile on CPython 3.11 for Ubuntu and macOS, then imports every advertised integration dependency. Native Windows is experimental. Linux-only integrations such as browser credential extraction and systemd units have narrower requirements than the core.",
    ),
    GuideClaim(
        "getting-started.b08",
        "docs/getting-started.md",
        28,
        "policy",
        "compatibility-policy",
        "<!-- compatibility-policy:end -->",
    ),
    GuideClaim(
        "getting-started.b09",
        "docs/getting-started.md",
        30,
        "policy",
        "prerequisites",
        "Confirm both commands are available:",
    ),
    GuideClaim(
        "getting-started.b10",
        "docs/getting-started.md",
        32,
        "manual",
        "manual-prerequisites",
        "```console\npython3 --version\nuv --version\n```",
    ),
    GuideClaim("getting-started.b11", "docs/getting-started.md", 37, "structure", "layout", "## Install"),
    GuideClaim(
        "getting-started.b12",
        "docs/getting-started.md",
        39,
        "manual",
        "manual-install",
        "Install the released base package:",
    ),
    GuideClaim(
        "getting-started.b13",
        "docs/getting-started.md",
        41,
        "manual",
        "manual-install",
        "```console\nuv tool install fieldkit-cli\n```",
    ),
    GuideClaim(
        "getting-started.b14",
        "docs/getting-started.md",
        45,
        "behavior",
        "version",
        "Confirm the installed package version:",
    ),
    GuideClaim(
        "getting-started.b15",
        "docs/getting-started.md",
        47,
        "behavior",
        "version",
        "```console\nfieldkit --version\n```",
    ),
    GuideClaim(
        "getting-started.b16",
        "docs/getting-started.md",
        51,
        "behavior",
        "version",
        "`fieldkit --version` reports the installed package version. To test a source\ncheckout, use the contributor path in\n[CONTRIBUTING.md](https://github.com/mpeter/fieldkit-cli/blob/main/CONTRIBUTING.md)\nand run `uv run fieldkit` from that checkout.",
    ),
    GuideClaim(
        "getting-started.b17",
        "docs/getting-started.md",
        56,
        "policy",
        "profiles",
        "To install a profile instead of the base package, choose one of these commands:",
    ),
    GuideClaim(
        "getting-started.b18",
        "docs/getting-started.md",
        58,
        "manual",
        "manual-profile",
        "```console\nuv tool install 'fieldkit-cli[google]'      # Google APIs and OAuth\nuv tool install 'fieldkit-cli[llm]'         # supported AI providers\nuv tool install 'fieldkit-cli[web]'         # local web interface\nuv tool install 'fieldkit-cli[chrome-auth]' # supported browser credential stores\nuv tool install 'fieldkit-cli[all]'         # all packaged optional dependencies\n```",
    ),
    GuideClaim(
        "getting-started.b19",
        "docs/getting-started.md",
        66,
        "policy",
        "profile-authorization",
        "Installing a profile does not configure a provider or authorize fieldkit to use\none. If fieldkit is already installed, add `--force` to replace its tool\nenvironment with the selected profile. See [Integrations and\nprofiles](integrations.md) before enabling a service.",
    ),
    GuideClaim(
        "getting-started.b20", "docs/getting-started.md", 71, "structure", "layout", "## Create your first workspace"
    ),
    GuideClaim(
        "getting-started.b21",
        "docs/getting-started.md",
        73,
        "policy",
        "operator",
        "Choose a directory whose contents fieldkit may create or update, then run:",
    ),
    GuideClaim(
        "getting-started.b22",
        "docs/getting-started.md",
        75,
        "behavior",
        "minimal",
        "```console\nfieldkit init --minimal ./fieldkit-workspace\n```",
    ),
    GuideClaim(
        "getting-started.b23",
        "docs/getting-started.md",
        79,
        "behavior",
        "minimal",
        "Minimal initialization is non-interactive. It creates generic workspace\nstructure without writing identity values, service credentials, or sample\ncustomer records. It also writes or updates `~/.config/fieldkit/config.yaml`\n(or `$XDG_CONFIG_HOME/fieldkit/config.yaml` when `XDG_CONFIG_HOME` is absolute),\nmaking this directory the active workspace and setting its database paths.\nOther existing configuration keys are preserved.",
    ),
    GuideClaim(
        "getting-started.b24",
        "docs/getting-started.md",
        86,
        "behavior",
        "trial",
        "For a disposable trial that does not read file contents from or modify your normal\nconfiguration, workspace, or runtime-data root, run the commands in a subshell:",
    ),
    GuideClaim(
        "getting-started.b25",
        "docs/getting-started.md",
        89,
        "behavior",
        "trial",
        '```console\nFIELDKIT_TRIAL_ROOT="$(mktemp -d)"\n(\n  export XDG_CONFIG_HOME="$FIELDKIT_TRIAL_ROOT/config"\n  export FIELDKIT_DATA_DIR="$FIELDKIT_TRIAL_ROOT/runtime-data"\n  export PYTHON_DOTENV_DISABLED=1\n  unset GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET\n  unset FIELDKIT_SKILLS_DIR\n  fieldkit init --minimal "$FIELDKIT_TRIAL_ROOT/workspace"\n  fieldkit doctor\n  fieldkit skill list\n)\necho "$FIELDKIT_TRIAL_ROOT"\n```',
    ),
    GuideClaim(
        "getting-started.b26",
        "docs/getting-started.md",
        104,
        "policy",
        "operator",
        "The `echo` command prints the temporary directory containing the trial\nconfiguration, workspace, and runtime data. The subshell restores your existing\nenvironment when it exits. Verify the printed path before removing it.",
    ),
    GuideClaim(
        "getting-started.b27",
        "docs/getting-started.md",
        108,
        "policy",
        "operator",
        "If you already use fieldkit, choose the workspace you intend to make active or\nback up that configuration file before trying a different workspace.",
    ),
    GuideClaim(
        "getting-started.b28", "docs/getting-started.md", 111, "structure", "layout", "## Verify the installation"
    ),
    GuideClaim(
        "getting-started.b29",
        "docs/getting-started.md",
        113,
        "behavior",
        "trial",
        "```console\nfieldkit doctor\nfieldkit skill list\n```",
    ),
    GuideClaim(
        "getting-started.b30",
        "docs/getting-started.md",
        118,
        "behavior",
        "doctor",
        "`fieldkit doctor` exits successfully when the services it checks are healthy or\nunconfigured and their checked settings are valid. It does not validate every\nLLM, MCP, or driver setting. In the isolated trial above, Salesforce, Gmail,\nGoogle, and ShadowBot are reported as disabled or not configured. If an\nintegration is enabled instead, fieldkit found existing configuration or\ncredentials. With no `FIELDKIT_SKILLS_DIR` or configured `fieldkit_root`\noverride, `fieldkit skill list` verifies packaged workflow resources can be\ndiscovered. With the isolated roots and cleared credentials shown above,\nneither command contacts an external provider.",
    ),
    GuideClaim(
        "getting-started.b31",
        "docs/getting-started.md",
        128,
        "navigation",
        "navigation",
        "If either command fails, keep the exact command, exit code, and sanitized error\ntext, then use [Troubleshooting](reference/troubleshooting.md) or the route in\n[SUPPORT.md](https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md).",
    ),
    GuideClaim(
        "getting-started.b32", "docs/getting-started.md", 132, "structure", "layout", "## Keep or replace the workspace"
    ),
    GuideClaim(
        "getting-started.b33",
        "docs/getting-started.md",
        134,
        "behavior",
        "minimal",
        "An initially empty, newly created workspace contains only the generic files\ncreated by minimal initialization unless you add data. Existing workspace files\nare preserved. You can keep it as your working directory. Before removing\na non-disposable workspace, initialize another workspace or update your fieldkit\nconfiguration so it does not retain paths into a deleted directory.",
    ),
    GuideClaim(
        "getting-started.b34",
        "docs/getting-started.md",
        140,
        "manual",
        "manual-uninstall",
        "Uninstalling the application removes the tool environment and launcher. It does\nnot remove workspaces, runtime data, or the active fieldkit configuration:",
    ),
    GuideClaim(
        "getting-started.b35",
        "docs/getting-started.md",
        143,
        "manual",
        "manual-uninstall",
        "```console\nuv tool uninstall fieldkit-cli\n```",
    ),
    GuideClaim(
        "getting-started.b36", "docs/getting-started.md", 147, "structure", "layout", "## Configure a real workspace"
    ),
    GuideClaim(
        "getting-started.b37",
        "docs/getting-started.md",
        149,
        "policy",
        "operator",
        "Run the interactive setup only when you are ready to supply identity, workspace,\nand optional integration settings:",
    ),
    GuideClaim(
        "getting-started.b38",
        "docs/getting-started.md",
        152,
        "manual",
        "manual-interactive",
        "```console\nfieldkit init\nfieldkit doctor\n```",
    ),
    GuideClaim(
        "getting-started.b39",
        "docs/getting-started.md",
        157,
        "policy",
        "answers-guidance",
        "Review [Workspace initialization](guides/init.md) first if you need unattended\nsetup. Treat its answers file as sensitive when it contains identity or OAuth\nvalues.",
    ),
    GuideClaim("getting-started.b40", "docs/getting-started.md", 161, "structure", "layout", "## Next steps"),
    GuideClaim(
        "getting-started.b41",
        "docs/getting-started.md",
        163,
        "navigation",
        "navigation",
        "- Learn the [main workflows](user-guide.md).\n- Understand [where data lives and what can leave the machine](privacy.md).\n- Configure only the [integration profiles](integrations.md) you need.\n- Use the generated [CLI reference](cli-reference.md) for exact options.",
    ),
)

EVIDENCE_NODES = {
    "minimal": FIRST_USER_GUIDE_NODES[1:5],
    "trial": (FIRST_USER_GUIDE_NODES[0], *FIRST_USER_GUIDE_NODES[13:19]),
    "doctor": FIRST_USER_GUIDE_NODES[5:9] + FIRST_USER_GUIDE_NODES[11:13],
    "version": (FIRST_USER_GUIDE_NODES[19],),
    "profiles": FIRST_USER_GUIDE_NODES[9:11] + FIRST_USER_GUIDE_NODES[34:37],
    "profile-authorization": (FIRST_USER_GUIDE_NODES[35], FIRST_USER_GUIDE_NODES[0]),
    "compatibility": (FIRST_USER_GUIDE_NODES[37],),
    "contributor": (FIRST_USER_GUIDE_NODES[38],),
    "manual-install": FIRST_USER_GUIDE_NODES[19:22] + FIRST_USER_GUIDE_NODES[22:25],
    "manual-profile": FIRST_USER_GUIDE_NODES[20:22],
    "manual-prerequisites": (FIRST_USER_GUIDE_NODES[19],),
    "capabilities": FIRST_USER_GUIDE_NODES[25:36] + FIRST_USER_GUIDE_NODES[39:43],
}
POLICY_SOURCES = {
    "product": ("docs/user-guide.md", "docs/concepts.md"),
    "publication": ("docs/releases.md", "RELEASING.md", "docs/release-readiness/release-governance-policy.json"),
    "prerequisites": ("pyproject.toml", "docs/compatibility.md"),
    "compatibility-policy": ("docs/release-readiness/compatibility-policy.json", "docs/compatibility.md"),
    "dependency-policy": ("docs/dependency-security.md", "docs/release-readiness/dependency-security-policy.json"),
    "compatibility": (".github/workflows/compatibility.yml", "docs/compatibility.md"),
    "profiles": ("pyproject.toml", "docs/integrations.md"),
    "profile-authorization": ("docs/integrations.md", "docs/privacy.md"),
    "contributor": ("CONTRIBUTING.md", "Makefile"),
    "answers-guidance": ("docs/guides/init.md",),
    "operator": ("docs/guides/init.md", "docs/privacy.md"),
}
# Local tests of artifact runner construction do not establish a real install,
# hosted matrix result, immutable publication, or external tool lifecycle.
PENDING_AUTHORITIES = {
    "manual-install": "actual-installed-artifact-and-published-index",
    "manual-profile": "actual-installed-profile-and-uv-tool-replacement",
    "manual-prerequisites": "operator-python-and-uv-installation",
    "manual-contributor": "external-clone-and-contributor-gate",
    "manual-uninstall": "external-uv-tool-uninstall-and-operator-retention",
    "manual-interactive": "operator-confirmation-and-optional-credentialed-setup",
    "publication": "immutable-release-record",
    "compatibility": "current-revision-hosted-matrix-and-container",
    "compatibility-policy": "current-revision-hosted-matrix-and-container",
    "prerequisites": "operator-python-and-uv-installation",
    "version": "actual-installed-artifact-version-and-source-launcher",
    "profile-authorization": "external-installer-downloads-and-uv-tool-replacement",
    "contributor": "external-clone-and-clean-committed-contributor-gate",
    "navigation": "public-route-availability-and-maintainer-ownership",
}


@dataclass(frozen=True)
class CapabilityProof:
    claim: str
    selectors: tuple[str, ...]


CAPABILITY_PROOFS = (
    CapabilityProof(
        "- Create and validate local pursuit workspaces.",
        (
            "tests/test_pursuit_create.py::test_create_cmd_creates_pursuit_file",
            "tests/test_pursuit_create.py::test_create_cmd_exits_nonzero_missing_account_dir",
            "tests/test_init_guide_contract.py::test_real_minimal_init_and_preserving_rerun",
            "tests/test_init_guide_contract.py::test_real_invalid_answers_leave_every_file_unchanged",
        ),
    ),
    CapabilityProof(
        "- Audit, forecast, and advance structured opportunity notes.",
        (
            "tests/test_pipeline_workflow_guide_contract.py::test_real_audit_json_has_unavailable_qualification_and_no_writes",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_forecast_scopes_account_preserves_acv_precedence_and_overrides_quota",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_advance_pending_then_explicit_override_preserves_historical_data",
        ),
    ),
    CapabilityProof(
        "- Run local watchers and assemble a morning brief.",
        (
            "tests/test_watcher_guide_contract.py::test_real_unconfigured_aggregate_runs_four_locals_and_nonempty_no_llm_brief",
            "tests/test_brief_documentation_scenarios.py::test_documented_morning_brief_previews_are_offline_and_read_only",
        ),
    ),
    CapabilityProof(
        "- Add Salesforce and Gmail context when you configure those services.",
        (
            "tests/test_pipeline_sf_sync_documentation.py::test_documented_listview_json_format_does_not_choose_write_authority",
            "tests/test_gmail_published_sync.py::test_complete_provider_page_publishes_rows_and_checkpoint_together",
        ),
    ),
    CapabilityProof(
        "- Enable AI-assisted, local web, browser-auth, or organization-provided\n  capabilities independently.",
        (
            "tests/test_installation_profiles.py::test_profile_success_surface",
            "tests/test_installation_profiles.py::test_base_profile_reports_actionable_missing_profile",
            "tests/test_documentation_integration_profiles.py::test_integration_profile_table_matches_installation_and_import_contract",
            "tests/test_documentation_integration_profiles.py::test_google_library_installation_does_not_supply_oauth_credentials",
        ),
    ),
    CapabilityProof(
        "- Expose stable text, Markdown, and JSON output for scripts and agents.",
        (
            "tests/test_documentation_pursuit_workflow.py::test_documentation_audit_output_and_default_report",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_pipeline_generation_and_open_use_the_same_scope_without_a_viewer",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_audit_json_has_unavailable_qualification_and_no_writes",
        ),
    ),
)


def assert_capability_proofs(mappings: tuple[CapabilityProof, ...]) -> None:
    """Require each advertised behavior's direct evidence, including output formats."""
    by_claim = {mapping.claim: mapping.selectors for mapping in mappings}
    assert len(mappings) == len(by_claim) == 6
    assert len({mapping.selectors for mapping in mappings}) == 6
    assert by_claim == {
        "- Create and validate local pursuit workspaces.": (
            "tests/test_pursuit_create.py::test_create_cmd_creates_pursuit_file",
            "tests/test_pursuit_create.py::test_create_cmd_exits_nonzero_missing_account_dir",
            "tests/test_init_guide_contract.py::test_real_minimal_init_and_preserving_rerun",
            "tests/test_init_guide_contract.py::test_real_invalid_answers_leave_every_file_unchanged",
        ),
        "- Audit, forecast, and advance structured opportunity notes.": (
            "tests/test_pipeline_workflow_guide_contract.py::test_real_audit_json_has_unavailable_qualification_and_no_writes",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_forecast_scopes_account_preserves_acv_precedence_and_overrides_quota",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_advance_pending_then_explicit_override_preserves_historical_data",
        ),
        "- Run local watchers and assemble a morning brief.": (
            "tests/test_watcher_guide_contract.py::test_real_unconfigured_aggregate_runs_four_locals_and_nonempty_no_llm_brief",
            "tests/test_brief_documentation_scenarios.py::test_documented_morning_brief_previews_are_offline_and_read_only",
        ),
        "- Add Salesforce and Gmail context when you configure those services.": (
            "tests/test_pipeline_sf_sync_documentation.py::test_documented_listview_json_format_does_not_choose_write_authority",
            "tests/test_gmail_published_sync.py::test_complete_provider_page_publishes_rows_and_checkpoint_together",
        ),
        "- Enable AI-assisted, local web, browser-auth, or organization-provided\n  capabilities independently.": (
            "tests/test_installation_profiles.py::test_profile_success_surface",
            "tests/test_installation_profiles.py::test_base_profile_reports_actionable_missing_profile",
            "tests/test_documentation_integration_profiles.py::test_integration_profile_table_matches_installation_and_import_contract",
            "tests/test_documentation_integration_profiles.py::test_google_library_installation_does_not_supply_oauth_credentials",
        ),
        "- Expose stable text, Markdown, and JSON output for scripts and agents.": (
            "tests/test_documentation_pursuit_workflow.py::test_documentation_audit_output_and_default_report",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_pipeline_generation_and_open_use_the_same_scope_without_a_viewer",
            "tests/test_pipeline_workflow_guide_contract.py::test_real_audit_json_has_unavailable_qualification_and_no_writes",
        ),
    }


def semantic_blocks(text: str) -> tuple[str, ...]:
    """Keep every nonblank block, including unsupported syntax and HTML markers."""
    separated = re.sub(r"(<!-- compatibility-policy:(?:start|end) -->)", r"\n\n\1\n\n", text.strip())
    return tuple(block for block in re.split(r"\n\s*\n", separated) if block.strip())


def assert_guide(page: str, text: str) -> tuple[str, ...]:
    """Any visible or hidden source change requires renewed claim review."""
    actual = semantic_blocks(text)
    assert actual == tuple(claim.text for claim in INVENTORY if claim.page == page), (
        "first-user semantic inventory changed"
    )
    return actual


@pytest.mark.parametrize("page,count,fences", (("README.md", 29, 4), ("docs/getting-started.md", 41, 9)))
def test_complete_source_mapped_first_user_inventory(page: str, count: int, fences: int) -> None:
    """Every source block has a stable ID, source line, classification and owner."""
    text = (REPO / page).read_text(encoding="utf-8")
    blocks = assert_guide(page, text)
    claims = tuple(claim for claim in INVENTORY if claim.page == page)
    assert len(blocks) == count
    assert sum(block.startswith("```console") for block in blocks) == fences
    assert len({claim.identifier for claim in claims}) == count
    for claim in claims:
        assert claim.line == text[: text.index(claim.text)].count("\n") + 1
        assert claim.classification in {"structure", "behavior", "navigation", "policy", "manual"}
        assert (
            claim.evidence in EVIDENCE_NODES
            or claim.evidence in POLICY_SOURCES
            or claim.evidence
            in {
                "layout",
                "metadata",
                "navigation",
                "license",
                *PENDING_AUTHORITIES,
            }
        )
        if claim.classification == "behavior":
            assert claim.evidence in EVIDENCE_NODES
        if claim.classification == "manual":
            assert claim.evidence in PENDING_AUTHORITIES


def test_frontmatter_and_fixed_test_manifest_cover_declared_inputs() -> None:
    """Metadata and selectors are real inputs, not passing publication evidence."""
    metadata = yaml.safe_load(INVENTORY[29].text.split("---", 2)[1])
    assert metadata["audience"] == "user"
    assert metadata["last_reviewed"] == date(2026, 9, 29)
    assert metadata["covers"] == [
        "pyproject.toml",
        "src/fieldkit/__main__.py",
        "src/fieldkit/commands/init/",
        "src/fieldkit/commands/doctor/",
        "src/fieldkit/config/",
        "src/fieldkit/commands/skill/",
        "src/fieldkit/skill/",
        "src/fieldkit/skills/",
    ]
    assert all((REPO / path).exists() for path in metadata["covers"])
    assert len(FIRST_USER_GUIDE_NODES) == len(set(FIRST_USER_GUIDE_NODES)) == 43
    assert len({node.split("::", 1)[0] for node in FIRST_USER_GUIDE_NODES}) == 18
    for node in FIRST_USER_GUIDE_NODES:
        file, _, name = node.partition("::")
        tree = ast.parse((REPO / file).read_text(encoding="utf-8"))
        if name:
            assert name in {
                item.name for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    for paths in POLICY_SOURCES.values():
        assert all((REPO / path).is_file() for path in paths)
    assert all(node in FIRST_USER_GUIDE_NODES for nodes in EVIDENCE_NODES.values() for node in nodes)
    assert {node for nodes in EVIDENCE_NODES.values() for node in nodes} == set(FIRST_USER_GUIDE_NODES)
    assert PENDING_AUTHORITIES["compatibility"] == "current-revision-hosted-matrix-and-container"


@pytest.mark.parametrize("index", range(70), ids=tuple(claim.identifier for claim in INVENTORY))
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    """No source position can add, lose, negate, duplicate or reorder a promise."""
    claim = INVENTORY[index]
    claims = [item for item in INVENTORY if item.page == claim.page]
    position = claims.index(claim)
    blocks = [item.text for item in claims]
    if mutation == "insert":
        blocks.insert(position, "<aside>Unreviewed first-use promise.</aside>")
    elif mutation == "remove":
        blocks.pop(position)
    elif mutation == "alter":
        blocks[position] += " changed"
    elif mutation == "negate":
        blocks[position] = "Not " + blocks[position]
    elif mutation == "duplicate":
        blocks.insert(position, blocks[position])
    else:
        other = (position + 1) % len(blocks)
        blocks[position], blocks[other] = blocks[other], blocks[position]
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide(claim.page, "\n\n".join(blocks))


@pytest.mark.parametrize(
    "claim", tuple(claim for claim in INVENTORY if claim.text.startswith("```")), ids=lambda claim: claim.identifier
)
@pytest.mark.parametrize("mutation", ("language", "body"))
def test_all_thirteen_fence_mutations_are_rejected(claim: GuideClaim, mutation: str) -> None:
    """Fence language and exact commands remain independently reviewable."""
    lines = claim.text.splitlines()
    lines[0 if mutation == "language" else 1] += "-unsupported"
    text = "\n\n".join(
        item.text if item != claim else "\n".join(lines) for item in INVENTORY if item.page == claim.page
    )
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide(claim.page, text)


SEMANTIC_PARTS = tuple(
    (claim, token)
    for claim in INVENTORY
    for token in (
        *re.findall(r"(?<!`)`[^`\n]+`(?!`)", claim.text),
        *re.findall(r"\[[^\]]+\]\([^\)]+\)", claim.text),
        *re.findall(r"(?m)^- .*(?:\n {2}.*)*", claim.text),
    )
)


@pytest.mark.parametrize("claim,token", SEMANTIC_PARTS)
def test_inline_list_and_link_mutations_are_rejected(claim: GuideClaim, token: str) -> None:
    """Inline syntax, each list item and each destination cannot silently drift."""
    changed = claim.text.replace(token, token + "-unsupported", 1)
    text = "\n\n".join(item.text if item != claim else changed for item in INVENTORY if item.page == claim.page)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide(claim.page, text)


@pytest.mark.parametrize("page", ("README.md", "docs/getting-started.md"))
@pytest.mark.parametrize(
    "hidden",
    (
        "[hidden]: https://example.com/unsupported",
        "<!-- hidden promise -->",
        "<script>unknown()</script>",
        "~~~console\nunknown\n~~~",
    ),
)
def test_unsupported_and_hidden_reference_syntax_is_rejected(page: str, hidden: str) -> None:
    """Unknown definitions and HTML are retained rather than discarded by parsing."""
    text = "\n\n".join(claim.text for claim in INVENTORY if claim.page == page)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide(page, text + "\n\n" + hidden)


def test_readme_capabilities_have_six_distinct_behavior_mappings() -> None:
    """Each exact subclaim selects an independently relevant direct behavior."""
    capabilities = re.findall(r"(?m)^- .*(?:\n {2}.*)*", INVENTORY[12].text)
    assert tuple(mapping.claim for mapping in CAPABILITY_PROOFS) == tuple(capabilities)
    assert len(CAPABILITY_PROOFS) == 6
    for mapping in CAPABILITY_PROOFS:
        assert mapping.selectors
        assert set(mapping.selectors) <= set(FIRST_USER_GUIDE_NODES)
    assert_capability_proofs(CAPABILITY_PROOFS)


@pytest.mark.parametrize("mutation", ("swap", "drop", "misroute"))
def test_capability_mapping_negative_controls(mutation: Literal["swap", "drop", "misroute"]) -> None:
    mappings = list(CAPABILITY_PROOFS)
    if mutation == "swap":
        mappings[0] = CapabilityProof(mappings[0].claim, mappings[1].selectors)
        mappings[1] = CapabilityProof(mappings[1].claim, CAPABILITY_PROOFS[0].selectors)
    elif mutation == "drop":
        mappings.pop(2)
    else:
        mappings[3] = CapabilityProof(mappings[3].claim, mappings[1].selectors)
    with pytest.raises(AssertionError):
        assert_capability_proofs(tuple(mappings))


SECONDARY_CAPABILITY_SELECTORS = tuple(
    (index, selector) for index, mapping in enumerate(CAPABILITY_PROOFS) for selector in mapping.selectors[1:]
)


@pytest.mark.parametrize("index,selector", SECONDARY_CAPABILITY_SELECTORS)
@pytest.mark.parametrize("mutation", ("drop", "misroute"))
def test_secondary_capability_selector_negative_controls(index: int, selector: str, mutation: str) -> None:
    mappings = list(CAPABILITY_PROOFS)
    selected = list(mappings[index].selectors)
    selected.remove(selector)
    if mutation == "misroute":
        selected.append(CAPABILITY_PROOFS[(index + 1) % len(mappings)].selectors[0])
    mappings[index] = CapabilityProof(mappings[index].claim, tuple(selected))
    with pytest.raises(AssertionError):
        assert_capability_proofs(tuple(mappings))


def test_trial_inventory_rejects_broad_no_read_promise() -> None:
    text = "\n\n".join(claim.text for claim in INVENTORY if claim.page == "docs/getting-started.md")
    old = text.replace(
        "does not read file contents from or modify your normal", "does not read or modify your normal fieldkit"
    )
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("docs/getting-started.md", old)


def test_dependency_downloads_are_distinct_from_workspace_data_and_authorization() -> None:
    """The former network-free installation promise is explicitly rejected."""
    text = "\n\n".join(claim.text for claim in INVENTORY if claim.page == "README.md")
    assert "downloads its Python dependencies" in text
    assert "transmit your fieldkit workspace data" in text
    old = text.replace("downloads its Python dependencies", "provides its Python dependencies").replace(
        "transmit your fieldkit workspace data",
        "send data anywhere",
    )
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("README.md", old)


def test_all_local_navigation_targets_exist() -> None:
    """Reviewed local destinations must resolve inside the public source tree."""
    destinations = tuple(
        (claim.page, target)
        for claim in INVENTORY
        for target in re.findall(r"\]\(([^)]+)\)", claim.text)
        if not target.startswith(("https://", "http://"))
    )
    assert destinations
    for page, target in destinations:
        path, _, anchor = target.partition("#")
        destination = (REPO / page).parent / path
        assert destination.resolve().is_relative_to(REPO)
        assert destination.is_file(), target
        if anchor:
            headings = re.findall(r"(?m)^#+ (.+)$", destination.read_text(encoding="utf-8"))
            assert anchor in {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-") for heading in headings}


# The guard belongs only to this test process. It is installed before fieldkit
# imports and protects synthetic normal state, not the operator's actual files.
TRIAL_CHILD = r"""
import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from importlib import resources
from pathlib import Path

root = Path(sys.argv[1])
normal = root / "normal"
trial = root / "trial"
mode = sys.argv[2]
violations = []

def audit(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.sendto", "socket.getaddrinfo",
                 "subprocess.Popen", "os.system", "os.posix_spawn"}:
        violations.append(event)
        raise AssertionError("trial attempted provider or process access")
    if event in {"open", "os.listdir", "os.scandir", "os.remove", "os.rmdir", "os.mkdir",
                 "os.rename", "os.chmod", "os.truncate"}:
        for value in args[:2] if event == "os.rename" else args[:1]:
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).resolve()
                if path.is_relative_to(normal) or path == root / ".env":
                    violations.append(event)
                    raise AssertionError("trial attempted protected normal-state access")

sys.addaudithook(audit)
if mode == "guard-read":
    (normal / "workspace" / "account.md").read_text(encoding="utf-8")
if mode == "guard-write":
    (normal / "workspace" / "account.md").write_text("changed", encoding="utf-8")
if mode == "guard-list":
    list((normal / "workspace").iterdir())

from fieldkit.__main__ import main
from fieldkit.config import CONFIG_PATH, get_fieldkit_data, get_fieldkit_home
from fieldkit.commands.skill._runner import _skills_dir

def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = main(argv)
    assert status == 0, (status, stdout.getvalue(), stderr.getvalue())
    return stdout.getvalue()

initialized = invoke(["init", "--minimal", str(trial / "workspace")])
assert CONFIG_PATH == trial / "config" / "fieldkit" / "config.yaml", "trial config root leaked"
assert get_fieldkit_home() == trial / "workspace", "trial workspace root leaked"
assert get_fieldkit_data() == trial / "runtime-data", "trial runtime root leaked"
doctor = invoke(["doctor"])
assert all(name in doctor for name in ("sf:", "gmail:", "google:", "shadowbot:")), doctor
assert doctor.count("optional — not configured") == 4, doctor
skills = invoke(["skill", "list"])
assert _skills_dir() == Path(str(resources.files("fieldkit.skills"))), "external skill selector leaked"
assert "brief" in skills and "outside-sentinel" not in skills, skills
assert not violations, violations
sys.stdout.write(json.dumps({"initialized": initialized, "doctor": doctor, "skills": skills,
                            "config": str(CONFIG_PATH), "workspace": str(get_fieldkit_home()),
                            "runtime": str(get_fieldkit_data()), "violations": violations}))
"""


@dataclass(frozen=True)
class Trial:
    root: Path
    environment: dict[str, str]
    before: dict[str, tuple[int, int, bytes | None]]


@pytest.fixture
def isolated_trial(tmp_path: Path) -> Trial:
    """Retain realistic normal selectors while applying the documented subshell."""
    normal = tmp_path / "normal"
    home = tmp_path / "home"
    home.mkdir()
    for name in ("config/fieldkit", "workspace", "runtime-data", "external-skills/outside-sentinel"):
        (normal / name).mkdir(parents=True)
    (home / ".config").symlink_to(normal / "config", target_is_directory=True)
    (normal / "workspace" / "account.md").write_text("Operator-owned fictional note.\n", encoding="utf-8")
    (normal / "config/fieldkit/config.yaml").write_text(
        f"fieldkit_home: {normal / 'workspace'}\nfieldkit_data: {normal / 'runtime-data'}\n"
        f"fieldkit_root: {normal / 'external-skills'}\n",
        encoding="utf-8",
    )
    (normal / "config/fieldkit/sf-cookies.json").write_text("{}\n", encoding="utf-8")
    (normal / "runtime-data/google-oauth-token.json").write_text("{}\n", encoding="utf-8")
    (normal / "external-skills/outside-sentinel/SKILL.md").write_text(
        "---\nname: outside-sentinel\ndescription: External synthetic workflow\n---\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "GOOGLE_OAUTH_CLIENT_ID=fictional-client\nGOOGLE_OAUTH_CLIENT_SECRET=fictional-secret\n",
        encoding="utf-8",
    )
    trial = tmp_path / "trial"
    trial.mkdir()
    # Build an allowlist rather than accidentally re-injecting operator selectors.
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(trial / "config"),
            "FIELDKIT_DATA_DIR": str(trial / "runtime-data"),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONPATH": str(REPO / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return Trial(tmp_path, environment, snapshot_workflow(normal))


def run_trial(trial: Trial, mode: str = "documented") -> subprocess.CompletedProcess[str]:
    """Execute fixed Python argv with fresh import-time path selection."""
    environment = dict(trial.environment)
    if mode == "config-isolation-removed":
        environment.pop("XDG_CONFIG_HOME")
    elif mode == "runtime-isolation-removed":
        environment["FIELDKIT_DATA_DIR"] = str(trial.root / "normal/runtime-data")
    elif mode == "dotenv-disable-removed":
        environment.pop("PYTHON_DOTENV_DISABLED")
    elif mode == "credentials-clearing-removed":
        environment.update(GOOGLE_OAUTH_CLIENT_ID="fictional-client", GOOGLE_OAUTH_CLIENT_SECRET="fictional-secret")
    elif mode == "skill-selector-clearing-removed":
        environment["FIELDKIT_SKILLS_DIR"] = str(trial.root / "normal/external-skills")
    return subprocess.run(
        [sys.executable, "-c", TRIAL_CHILD, str(trial.root), mode],
        cwd=trial.root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def test_real_documented_isolated_trial_preserves_normal_state(isolated_trial: Trial) -> None:
    """Real CLI children prove disabled diagnostics and bundled resource discovery."""
    result = run_trial(isolated_trial)
    assert result.returncode == 0, result.stderr[-8192:]
    payload = json.loads(result.stdout)
    assert payload["violations"] == []
    assert snapshot_workflow(isolated_trial.root / "normal") == isolated_trial.before
    workspace = isolated_trial.root / "trial/workspace"
    assert {path.name for path in workspace.iterdir()} == {"accounts", "config", "data"}
    assert not (workspace / "config/identity.yaml").exists()
    assert not (workspace / ".env").exists()


@pytest.mark.parametrize("mode", ("guard-read", "guard-write", "guard-list"))
def test_child_guard_rejects_protected_normal_state(isolated_trial: Trial, mode: str) -> None:
    """Negative attempts establish that reads, writes and enumeration are denied."""
    result = run_trial(isolated_trial, mode)
    assert result.returncode != 0
    assert "protected normal-state access" in result.stderr
    assert snapshot_workflow(isolated_trial.root / "normal") == isolated_trial.before


@pytest.mark.parametrize(
    "mode",
    (
        "config-isolation-removed",
        "runtime-isolation-removed",
        "dotenv-disable-removed",
        "credentials-clearing-removed",
        "skill-selector-clearing-removed",
    ),
)
def test_removing_documented_isolation_or_selector_clearing_fails(isolated_trial: Trial, mode: str) -> None:
    """Each meaningful subshell boundary must defeat synthetic ambient state."""
    result = run_trial(isolated_trial, mode)
    assert result.returncode != 0, result.stdout[-8192:]
    assert snapshot_workflow(isolated_trial.root / "normal") == isolated_trial.before
