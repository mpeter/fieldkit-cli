"""Whole-page initialization guide inventory and actual offline CLI scenarios."""

import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import pytest
import yaml
from dotenv import dotenv_values

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.commands.skill import _runner as skill_runner
from scripts.documentation_commands import DOCUMENT_COMMANDS
from tests.documentation_workflow_support import (
    deny_documentation_network as deny_documentation_network,
)
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow

pytestmark = pytest.mark.integration
PAGE = Path("docs/guides/init.md")
OWNER = "init_guide_contract"
INIT_GUIDE_NODES = (
    "tests/test_init_guide_contract.py",
    "tests/test_init_config_preservation.py",
    "tests/test_init_config_merge.py",
    "tests/test_init_wizard_funcs.py::test_load_answers_returns_typed_values_and_defaults",
    "tests/test_init_wizard_funcs.py::test_load_answers_rejects_invalid_document",
    "tests/test_init_wizard_funcs.py::test_account_key_normalizes_safe_name_and_rejects_traversal",
    "tests/test_init_wizard_extended.py::test_write_env_temp_is_restricted_before_publication",
    "tests/test_init_wizard_extended.py::test_write_env_preserves_exact_dotenv_values",
    "tests/test_init_wizard_extended.py::test_write_env_rejects_symlink_without_changing_target",
    "tests/test_init_wizard_run_wizard_orchestration.py::test_confirm_true_writes_then_post_setup_and_returns_0",
    "tests/test_init_wizard_run_wizard_orchestration.py::test_confirm_false_returns_0_without_write_or_post_setup",
    "tests/test_init_wizard_run_wizard_orchestration.py::test_answers_mode_does_not_start_an_interactive_skill_install",
    "tests/test_init_wizard_extended.py::test_wizard_post_setup_handles_skill_install_failure",
    "tests/test_skill_runner_dispatch.py::test_skills_dir_importlib_resources_failure_is_fixed_config_error",
    "tests/test_skill_runner_dispatch.py::test_skills_dir_rejects_non_directory_bundled_resource",
)


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    classification: Literal["structure", "guidance", "behavior", "navigation"]
    evidence: str
    text: str


# Literal reviewed page inventory; no nonblank source is silently discarded.
INVENTORY = (
    GuideClaim(
        "metadata",
        "structure",
        "layout",
        "---\nlast_reviewed: 2026-09-27\ncovers:\n  - src/fieldkit/commands/init/\naudience: user\n---",
    ),
    GuideClaim("title", "structure", "layout", "# Initialize a workspace"),
    GuideClaim(
        "minimal-guidance", "guidance", "operator", "Use minimal initialization for a credential-free first workspace:"
    ),
    GuideClaim(
        "minimal-commands",
        "behavior",
        "installed-base",
        "```console\nfieldkit init --minimal ./fieldkit-workspace\nfieldkit doctor\nfieldkit skill list\n```",
    ),
    GuideClaim(
        "minimal-scaffold",
        "behavior",
        "minimal-cli",
        "The initialization command creates `accounts`, `config`, and `data` directories\nin the selected workspace and writes a generic `config/accounts.yaml` account\nindex. It does not create unused briefing configuration files.",
    ),
    GuideClaim(
        "minimal-preservation",
        "behavior",
        "minimal-cli",
        "It also writes the active workspace and database paths to your fieldkit user\nconfiguration. Existing unrelated configuration keys are preserved. Rerunning\nminimal initialization creates missing scaffold files but does not replace an\nexisting account index. Unrelated operator files remain untouched.",
    ),
    GuideClaim(
        "configuration-failures",
        "behavior",
        "preservation",
        "If existing user configuration cannot be read as a YAML mapping, initialization\nstops with exit status `3` instead of replacing it. Preserve the file and repair\nthe reported configuration problem before retrying; do not delete it to force\ninitialization to proceed.\nThe wizard also checks existing account and identity YAML before creating its\nworkspace artifacts. Updates preserve unrelated fields and use atomic YAML\nreplacement; a failure is not permission to discard the existing files.",
    ),
    GuideClaim(
        "isolated-trial",
        "navigation",
        "links",
        "Unconfigured integrations are expected in the doctor output. For a disposable\ntrial that does not change your normal fieldkit configuration, use the isolated\nprocedure in [Installation and first success](../getting-started.md).",
    ),
    GuideClaim("interactive-heading", "structure", "layout", "## Configure identity and accounts interactively"),
    GuideClaim(
        "interactive-guidance",
        "guidance",
        "operator",
        "Run `fieldkit init` when you are ready to store identity, account names, and\noptional integration settings.",
    ),
    GuideClaim(
        "interactive-summary-preservation",
        "behavior",
        "wizard",
        "The wizard shows a summary before writing. It creates identity and account\nconfiguration, one directory per configured account, and the fieldkit user\nconfiguration. Existing account entries, account notes, and unrelated operator\nfiles are preserved. Review the selected workspace\nand every value in the summary before confirming.",
    ),
    GuideClaim(
        "interactive-skill-install",
        "behavior",
        "skill-install",
        "After writing the workspace, the interactive wizard attempts to install bundled\nskills into a detected agent tool. That step can update the tool's local project\nconfiguration and does not have a separate confirmation prompt. Use unattended\nsetup if you need to defer it; unattended setup prints the install command\ninstead. Skill installation is separate from read-only discovery with `fieldkit\nskill list`.",
    ),
    GuideClaim("answers-heading", "structure", "layout", "## Unattended setup"),
    GuideClaim(
        "answers-guidance",
        "guidance",
        "operator",
        "For a synthetic fixture or scripted onboarding, put the answers in a YAML file\nand pass it to init:",
    ),
    GuideClaim("answers-command", "behavior", "answers-cli", "```text\nfieldkit init --answers init-answers.yaml\n```"),
    GuideClaim(
        "required-answers",
        "behavior",
        "answers-cli",
        "The required keys are `name`, `email`, and `data_dir`. This example also creates\none account workspace:",
    ),
    GuideClaim(
        "answers-example",
        "behavior",
        "answers-cli",
        '```yaml\nname: Example User\nemail: user@example.com\ndata_dir: ./fieldkit-workspace\nrole: Account Executive\ncompany: Example Company\nterritory: East\nsalesforce_user_id: ""\naccounts:\n  - Acme Corp\n```',
    ),
    GuideClaim(
        "optional-answers-and-path",
        "behavior",
        "answers-cli",
        "Optional keys are `role`, `company`, `territory`, `salesforce_user_id`,\n`accounts`, `oauth_client_id`, `oauth_client_secret`, and\n`shadowbot_assistant_id`. Omit optional keys you do not use. An OAuth client\nsecret requires a client ID. A relative data directory is resolved from the\ndirectory where you run the command; use an absolute path when that context may\nvary.",
    ),
    GuideClaim(
        "account-slugs",
        "behavior",
        "answers-validation",
        "Account names may contain letters, digits, spaces, hyphens, and underscores;\nfieldkit converts spaces to hyphens for the workspace directory name.",
    ),
    GuideClaim(
        "answers-validation-before-write",
        "behavior",
        "answers-validation",
        "The answers document is validated before any configuration is written. Unknown\nkeys, missing required values, incorrect types, an unsafe account name, an OAuth\nsecret without a client ID, or an invalid ShadowBot assistant ID cause exit code\n3 and leave the workspace unchanged. A valid answers file skips stdin, the\nconfirmation prompt, and interactive agent-harness discovery. It writes the\nsame workspace and configuration artifacts as the interactive wizard, then\nprints the remaining post-setup guidance.",
    ),
    GuideClaim(
        "sensitive-answers-boundary",
        "behavior",
        "preservation",
        "An answers file containing identity or OAuth values is sensitive. Keep it\noutside the fieldkit source repository, restrict its permissions, and remove it\nwhen it is no longer needed.\nUse a regular UTF-8 YAML file no larger than 1 MiB. Initialization rejects\nsymlinked, unstable, unreadable, or oversized answers files without including\ntheir path or contents in parse-error diagnostics.\nDuplicate mapping keys are rejected rather than silently choosing the last\nvalue. Unknown-key and invalid-account diagnostics do not echo the supplied\nvalues.",
    ),
    GuideClaim(
        "private-dotenv-values",
        "behavior",
        "dotenv-cli",
        "Optional OAuth values are written to a private, mode-0600 `.env` file using\ndotenv syntax. This file is data for fieldkit's loader, not a shell script; do\nnot source it. Surrounding whitespace is trimmed from answer values. Literal\ndollar signs, quotes, backslashes, Unicode, and remaining internal LF newlines\nare preserved without variable interpolation. Remaining NUL and carriage-return\ncharacters are rejected before workspace artifacts are created.",
    ),
    GuideClaim("verify-heading", "structure", "layout", "## Verify what changed"),
    GuideClaim(
        "verification-and-source-root",
        "behavior",
        "source-root",
        "Initialization can update both the selected workspace and your fieldkit user\nconfiguration. After any mode completes, run `fieldkit doctor`, inspect the\nworkspace before adding real customer data, and read [Local data and\nprivacy](../privacy.md). Installing an optional dependency profile or running\ninitialization does not authorize an external service; each integration still\nrequires its own configuration and authentication.\nThe wizard records a discovered application root only when running from an\nidentified fieldkit source tree. An installed package does not add a guessed\ncheckout path; existing explicit overrides are preserved. With no\n`FIELDKIT_SKILLS_DIR` or configured `fieldkit_root` override, workflows are\ndiscovered through package resources.",
    ),
    GuideClaim(
        "existing-input-and-path-boundaries",
        "behavior",
        "preservation",
        "Initialization rejects malformed existing configuration rather than replacing\nit. Both modes re-read global configuration before merging; account and identity\nupdates retain unknown fields. Before creating artifacts, initialization rejects\npre-existing child destinations that redirect through symlinks or have\nincompatible file types. The selected workspace itself may be an alias to an\nexisting directory, but not a dangling symlink. Keep its\ndirectory tree stable while initialization runs: these checks do not guard\nagainst concurrent renames or roll back earlier artifacts after a later failure.",
    ),
)
EXPECTED_TEXT = tuple(claim.text for claim in INVENTORY)
EXPECTED_IDS = tuple(claim.identifier for claim in INVENTORY)
EVIDENCE_NODES = {
    "minimal-cli": ("tests/test_init_guide_contract.py::test_real_minimal_init_and_preserving_rerun",),
    "answers-cli": ("tests/test_init_guide_contract.py::test_real_unattended_answers_create_documented_artifacts",),
    "answers-validation": (
        "tests/test_init_guide_contract.py::test_real_invalid_answers_leave_every_file_unchanged",
        *INIT_GUIDE_NODES[3:6],
    ),
    "preservation": (INIT_GUIDE_NODES[1],),
    "dotenv-cli": (
        "tests/test_init_guide_contract.py::test_real_answers_publish_private_exact_dotenv",
        *INIT_GUIDE_NODES[6:9],
    ),
    "wizard": (INIT_GUIDE_NODES[9], INIT_GUIDE_NODES[10], INIT_GUIDE_NODES[1]),
    "skill-install": (INIT_GUIDE_NODES[11], INIT_GUIDE_NODES[12]),
    "source-root": (
        INIT_GUIDE_NODES[2],
        "tests/test_init_guide_contract.py::test_real_minimal_workspace_discovers_bundled_workflows",
        *INIT_GUIDE_NODES[13:15],
    ),
}


def semantic_blocks(text: str) -> tuple[str, ...]:
    """Preserve unsupported syntax and every nonblank block in source order."""
    return tuple(re.split(r"\n\s*\n", text.strip()))


def assert_guide(text: str) -> tuple[str, ...]:
    actual = semantic_blocks(text)
    assert actual == EXPECTED_TEXT, "init guide semantic inventory changed"
    return actual


def test_complete_ordered_init_guide_inventory() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert len(blocks) == len(INVENTORY) == len(set(EXPECTED_IDS)) == 25
    assert sum(block.startswith("```") for block in blocks) == 3
    assert {claim.evidence for claim in INVENTORY} == set(EVIDENCE_NODES) | {
        "layout",
        "operator",
        "links",
        "installed-base",
    }
    assert all(
        node in INIT_GUIDE_NODES or node.split("::", 1)[0] in INIT_GUIDE_NODES
        for nodes in EVIDENCE_NODES.values()
        for node in nodes
    )


def test_canonical_owner_executes_fixed_manifest() -> None:
    commands = DOCUMENT_COMMANDS.get(OWNER)
    assert commands is not None, "whole-page owner is not registered"
    assert len(commands) == 1
    argv = commands[0]
    assert argv[:3] == ("uv", "run", "pytest")
    assert argv[3:] == (*INIT_GUIDE_NODES, "-q", "-n", "0")


@pytest.mark.parametrize("index", range(len(INVENTORY)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    if mutation == "insert":
        blocks.insert(index, "<aside>Unreviewed promise.</aside>")
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
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("index", (3, 14, 16))
@pytest.mark.parametrize("mutation", ("language", "body"))
def test_fence_language_and_body_mutations_are_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    lines = blocks[index].splitlines()
    lines[0 if mutation == "language" else 1] += "-unsupported"
    blocks[index] = "\n".join(lines)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,inline",
    [
        (index, inline)
        for index, block in enumerate(EXPECTED_TEXT)
        for inline in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", block)
    ],
)
def test_inline_values_and_commands_cannot_drift(index: int, inline: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`unsupported`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,label,target",
    [
        (index, label, target)
        for index, block in enumerate(EXPECTED_TEXT)
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", block)
    ],
)
@pytest.mark.parametrize("mutation", ("label", "target"))
def test_link_labels_and_targets_cannot_drift(index: int, label: str, target: str, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    replacement = f"[unsupported]({target})" if mutation == "label" else f"[{label}](unknown.md)"
    blocks[index] = blocks[index].replace(f"[{label}]({target})", replacement)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


def test_reviewed_navigation_targets_exist() -> None:
    links = [target for block in EXPECTED_TEXT for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", block)]
    assert links == ["../getting-started.md", "../privacy.md"]
    assert all((PAGE.parent / target).is_file() for target in links)


@pytest.fixture
def isolated_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deny_documentation_network: None) -> Iterator[Path]:
    """Use real initialization with synthetic paths and deny prompts/tool discovery."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / ".config" / "fieldkit" / "config.yaml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("FIELDKIT_SKILLS_DIR", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.chdir(tmp_path)

    def reject_external(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unattended init attempted stdin or subprocess/tool discovery")

    monkeypatch.setattr("builtins.input", reject_external)
    monkeypatch.setattr(subprocess, "run", reject_external)
    monkeypatch.setattr(subprocess, "Popen", reject_external)
    config.clear_config_caches()
    skill_runner._skills_dir.cache_clear()
    yield tmp_path
    config.clear_config_caches()
    skill_runner._skills_dir.cache_clear()


def test_real_minimal_init_and_preserving_rerun(isolated_init: Path) -> None:
    root = isolated_init
    result = invoke_workflow(["init", "--minimal", "./fieldkit-workspace"])
    assert result.exit_code == 0, result.output
    workspace = root / "fieldkit-workspace"
    assert {path.name for path in workspace.iterdir()} == {"accounts", "config", "data"}
    accounts = workspace / "config" / "accounts.yaml"
    assert yaml.safe_load(accounts.read_text(encoding="utf-8")) == {"internal_domains": [], "accounts": {}}
    config_path = config.CONFIG_PATH
    settings = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert settings == {
        "fieldkit_home": str(workspace),
        "pipeline_db": str(workspace / "data" / "pipeline.db"),
        "gmail_db": str(workspace / "data" / "gmail.db"),
    }
    settings["operator"] = {"preserve": True}
    config_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    accounts.write_text("accounts:\n  acme-corp:\n    custom: keep\n", encoding="utf-8")
    note = workspace / "accounts" / "operator.md"
    note.write_text("Preserve this operator note.\n", encoding="utf-8")
    original = (accounts.read_bytes(), note.read_bytes())
    (workspace / "data").rmdir()
    result = invoke_workflow(["init", "--minimal", "./fieldkit-workspace"])
    assert result.exit_code == 0, result.output
    assert (workspace / "data").is_dir()
    assert (accounts.read_bytes(), note.read_bytes()) == original
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["operator"] == {"preserve": True}


def _write_answers(root: Path, updates: dict[str, object] | None = None) -> Path:
    answers = {
        "name": "Example User",
        "email": "user@example.com",
        "data_dir": "./fieldkit-workspace",
        "role": "Account Executive",
        "company": "Example Company",
        "territory": "East",
        "salesforce_user_id": "",
        "accounts": ["Acme Corp"],
    }
    answers.update(updates or {})
    path = root / "init-answers.yaml"
    path.write_text(yaml.safe_dump(answers), encoding="utf-8")
    return path


def test_real_minimal_workspace_discovers_bundled_workflows(isolated_init: Path) -> None:
    result = invoke_workflow(["init", "--minimal", "./fieldkit-workspace"])
    assert result.exit_code == 0, result.output
    config.clear_config_caches()
    selected = skill_runner._skills_dir()
    assert selected == Path(str(resources.files("fieldkit.skills")))
    before = snapshot_workflow(isolated_init)
    result = invoke_workflow(["skill", "list"])
    assert result.exit_code == 0, result.output
    assert "brief" in result.stdout
    assert snapshot_workflow(isolated_init) == before


def test_real_unattended_answers_create_documented_artifacts(isolated_init: Path) -> None:
    root = isolated_init
    answers = _write_answers(root)
    result = invoke_workflow(["init", "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    workspace = root / "fieldkit-workspace"
    identity = yaml.safe_load((workspace / "config" / "identity.yaml").read_text(encoding="utf-8"))
    assert identity["identity"]["name"] == "Example User"
    assert identity["identity"]["role"] == "Account Executive"
    assert (workspace / "accounts" / "acme-corp" / "account.md").is_file()
    assert yaml.safe_load((workspace / "config" / "accounts.yaml").read_text(encoding="utf-8"))["accounts"]["acme-corp"]
    assert not (workspace / ".env").exists()
    assert not list((workspace / "config").glob("*.json"))
    assert "fieldkit skill install" in result.stdout
    identity_path = workspace / "config" / "identity.yaml"
    identity["operator"] = {"keep": True}
    identity["identity"]["custom"] = "preserve"
    identity_path.write_text(yaml.safe_dump(identity), encoding="utf-8")
    accounts_path = workspace / "config" / "accounts.yaml"
    accounts = yaml.safe_load(accounts_path.read_text(encoding="utf-8"))
    accounts["operator"] = "keep"
    accounts["accounts"]["acme-corp"]["custom"] = "preserve"
    accounts_path.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    note = workspace / "accounts" / "acme-corp" / "account.md"
    note.write_text("# Operator-owned account notes\n", encoding="utf-8")
    original_note = note.read_bytes()
    result = invoke_workflow(["init", "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(identity_path.read_text(encoding="utf-8")) == identity
    assert yaml.safe_load(accounts_path.read_text(encoding="utf-8")) == accounts
    assert note.read_bytes() == original_note


@pytest.mark.parametrize(
    "updates",
    (
        {"unknown": "fictional"},
        {"name": ""},
        {"accounts": "Acme Corp"},
        {"accounts": ["../escape"]},
        {"oauth_client_secret": "fictional"},
        {"shadowbot_assistant_id": "bad/id"},
        {"oauth_client_id": "fictional", "oauth_client_secret": "remaining\0value"},
        {"oauth_client_id": "fictional", "oauth_client_secret": "remaining\rvalue"},
    ),
)
def test_real_invalid_answers_leave_every_file_unchanged(isolated_init: Path, updates: dict[str, object]) -> None:
    answers = _write_answers(isolated_init, updates)
    before = snapshot_workflow(isolated_init)
    result = invoke_workflow(["init", "--answers", str(answers)])
    assert result.exit_code == 3, result.output
    assert snapshot_workflow(isolated_init) == before
    assert not (isolated_init / "fieldkit-workspace").exists()
    assert not config.CONFIG_PATH.exists()


@pytest.mark.parametrize("secret", ("  $literal${UNCHANGED} a'b\"c\\path 雪\nline two  ",))
def test_real_answers_publish_private_exact_dotenv(isolated_init: Path, secret: str) -> None:
    answers = _write_answers(isolated_init, {"oauth_client_id": " fictional-client ", "oauth_client_secret": secret})
    result = invoke_workflow(["init", "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    path = isolated_init / "fieldkit-workspace" / ".env"
    assert path.stat().st_mode & 0o777 == 0o600
    assert dotenv_values(path, interpolate=False) == {
        "GOOGLE_OAUTH_CLIENT_ID": "fictional-client",
        "GOOGLE_OAUTH_CLIENT_SECRET": secret.strip(),
    }
    assert "export " not in path.read_text(encoding="utf-8")
