"""fieldkit init — wizard implementation.

Wizard orchestration and its contributor-facing helpers live here.
"""

import logging
import subprocess
import textwrap
from pathlib import Path

import click

import fieldkit.config as cfg
from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.commands.init.answers import (
    DEFAULT_COMPANY,
    DEFAULT_ROLE,
    SHADOWBOT_ASSISTANT_ID_RE,
    InitInputs,
    account_key,
    load_answers,
)
from fieldkit.commands.init.paths import bind_initialization_workspace, validate_initialization_paths
from fieldkit.config import TIMEOUT_HEALTH_CHECK
from fieldkit.config._accounts import read_accounts_mapping_for_update
from fieldkit.config._loader import read_config_mapping_for_update
from fieldkit.config._paths import validate_configured_fieldkit_root
from fieldkit.config.dotenv import validate_dotenv_values, write_dotenv_file
from fieldkit.config.source import discover_source_checkout
from fieldkit.util.atomic import atomic_yaml_write

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _prompt(label: str, default: str = "", required: bool = True) -> str:
    """Prompt the user for a value, showing default in brackets."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            raise SystemExit(EXIT_PARTIAL) from None
        value = raw or default
        if value or not required:
            return value
        click.echo("    ✗ This field is required.")


def _prompt_list(label: str, default: list[str]) -> list[str]:
    """Prompt for a comma-separated list, returning list[str]."""
    default_str = ", ".join(default)
    raw = _prompt(label, default=default_str, required=False)
    return [v.strip() for v in raw.split(",") if v.strip()] if raw else default


def _prompt_path(label: str, default: Path) -> Path:
    """Prompt for a filesystem path, expanding ~ and env vars."""
    raw = _prompt(label, default=str(default))
    return bind_initialization_workspace(Path(raw))


def _section(title: str) -> None:
    click.echo()
    click.echo(f"  ── {title} ──")


# ---------------------------------------------------------------------------
# Scaffold helpers
# ---------------------------------------------------------------------------


def _write_identity_yaml(data_dir: Path, identity: dict[str, object]) -> None:
    path = data_dir / "config" / "identity.yaml"
    existing = read_config_mapping_for_update(path)
    previous = _identity_fields(existing)
    atomic_yaml_write(path, {**existing, "identity": {**previous, **identity}})
    click.echo(f"    ✓ {path}")


def _write_accounts_yaml(data_dir: Path, account_names: list[str]) -> None:
    """Write a minimal accounts.yaml scaffold with one entry per account."""
    path = data_dir / "config" / "accounts.yaml"
    existing = read_accounts_mapping_for_update(path)

    existing_accounts: dict[str, object] = {}
    raw_accts = existing.get("accounts", {})
    if isinstance(raw_accts, dict):
        existing_accounts = raw_accts

    # Merge: keep existing entries, add stubs for new names
    for name in account_names:
        key = account_key(name)
        if key not in existing_accounts:
            existing_accounts[key] = {
                "domains": [],
                "team": [],
                "keywords": [name],
                "blindspots_min_messages": 20,
                "blindspot_days": 14,
            }

    out: dict[str, object] = dict(existing)
    out.setdefault("internal_domains", [])
    out["accounts"] = existing_accounts

    atomic_yaml_write(path, out)
    click.echo(f"    ✓ {path}")


def _write_account_stub(data_dir: Path, name: str) -> None:
    """Write a minimal account.md stub if one doesn't already exist."""
    key = account_key(name)
    acct_dir = data_dir / "accounts" / key
    acct_dir.mkdir(parents=True, exist_ok=True)

    for sub in ("pursuits", "meetings", "projects", "proposals"):
        (acct_dir / sub).mkdir(exist_ok=True)

    account_md = acct_dir / "account.md"
    if account_md.exists():
        return  # Don't overwrite existing data

    account_md.write_text(
        textwrap.dedent(f"""\
        ---
        account: {name}
        ---

        # {name}

        ## Overview

        [DATA NEEDED]

        ## Key Contacts

        | Name | Title | Role | Email |
        |------|-------|------|-------|
        |      |       |      |       |

        ## Qualification

        Current native qualification is unavailable until an opportunity is linked and read.
        Salesforce ClosePlan is the authoritative source.
    """),
        encoding="utf-8",
    )
    click.echo(f"    ✓ {account_md}")


def _write_env(data_dir: Path, oauth_id: str, oauth_secret: str) -> None:
    """Write optional OAuth credentials to <data_dir>/.env."""
    if not oauth_id and not oauth_secret:
        return
    env_path = data_dir / ".env"
    write_dotenv_file(env_path, {"GOOGLE_OAUTH_CLIENT_ID": oauth_id, "GOOGLE_OAUTH_CLIENT_SECRET": oauth_secret})
    click.echo(f"    ✓ {env_path}  (add to .gitignore if tracking this directory)")


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


def _load_existing_config() -> dict[str, object]:
    """Return existing config values as a dict for use as defaults."""
    data = read_config_mapping_for_update(cfg.CONFIG_PATH)
    if "fieldkit_root" in data:
        validate_configured_fieldkit_root(data["fieldkit_root"])
    return data


def _load_existing_identity(data_dir: Path) -> dict[str, object]:
    identity_path = data_dir / "config" / "identity.yaml"
    data = read_config_mapping_for_update(identity_path)
    return _identity_fields(data)


def _identity_fields(data: dict[str, object]) -> dict[str, object]:
    """Validate identity fields from one already-bound configuration snapshot."""
    inner = data.get("identity", {})
    if not isinstance(inner, dict) or any(not isinstance(key, str) for key in inner):
        raise cfg.ConfigError("Existing identity configuration must contain an identity mapping")
    return {key: value for key, value in inner.items() if isinstance(key, str)}


def _wizard_prompt_inputs(existing_cfg: dict[str, object]) -> InitInputs:
    """Prompt for every managed init value."""
    _section("Identity")
    role = _prompt("Role", default=str(existing_cfg.get("role", "") or DEFAULT_ROLE))
    company = _prompt("Company", default=str(existing_cfg.get("company", "") or DEFAULT_COMPANY))
    name = _prompt("Your full name", default=str(existing_cfg.get("name", "")))
    email = _prompt("Your work email", default=str(existing_cfg.get("email", "")))
    territory = _prompt(
        "Your sales territory (e.g., Named Accounts - West)",
        default=str(existing_cfg.get("territory", "")),
        required=False,
    )
    salesforce_user_id = _prompt(
        "Salesforce user ID (e.g., 005Dn000001abcD)",
        default=str(existing_cfg.get("salesforce_user_id", "")),
        required=False,
    )

    default_data_dir = Path(str(existing_cfg.get("fieldkit_home") or "~/fieldkit-workspace")).expanduser()

    _section("Accounts")
    click.echo("  Enter the account names you cover (comma-separated).")
    click.echo("  Example: Acme Corp, Example Bank")
    existing_identity = _load_existing_identity(default_data_dir)
    existing_accounts_list: list[str] = []
    raw_accts = existing_identity.get("accounts", [])
    if isinstance(raw_accts, list):
        existing_accounts_list = [str(a) for a in raw_accts]
    account_names = _prompt_list("Account names", default=existing_accounts_list)
    for account in account_names:
        account_key(account)

    _section("Data directory")
    click.echo("  This is where your accounts, pursuits, and meeting notes are stored.")
    click.echo("  It can be a new directory (will be created) or an existing one.")
    data_dir = _prompt_path("Data directory", default=default_data_dir)

    _section("Google OAuth (optional — for Gmail/Drive MCP server)")
    click.echo("  Obtain from: Google Cloud Console → APIs & Services → Credentials")
    click.echo("  Leave blank to skip and configure later.")
    oauth_id = _prompt("OAuth client ID", default="", required=False)
    oauth_secret = _prompt("OAuth client secret", default="", required=False) if oauth_id else ""

    _section("ShadowBot (optional integration)")
    click.echo("  Leave blank to skip and configure later.")
    while True:
        shadowbot_assistant_id = _prompt("ShadowBot assistant ID", default="", required=False)
        if not shadowbot_assistant_id:
            break
        if SHADOWBOT_ASSISTANT_ID_RE.match(shadowbot_assistant_id):
            break
        click.echo(
            "  ✗ Invalid assistant ID - only letters, digits, underscores, hyphens, and dots allowed (1-128 chars).",
            err=True,
        )
        click.echo("  Please try again.", err=True)

    return InitInputs(
        role=role,
        company=company,
        name=name,
        email=email,
        territory=territory,
        salesforce_user_id=salesforce_user_id,
        data_dir=data_dir,
        account_names=tuple(account_names),
        oauth_id=oauth_id,
        oauth_secret=oauth_secret,
        shadowbot_assistant_id=shadowbot_assistant_id,
    )


def _wizard_confirm(
    name: str,
    email: str,
    role: str,
    company: str,
    territory: str,
    salesforce_user_id: str,
    account_names: list[str],
    data_dir: Path,
    oauth_id: str,
    shadowbot_assistant_id: str = "",
) -> bool | None:
    """Print summary and prompt for confirmation. Returns True=proceed, False=cancel, None=abort."""
    click.echo()
    click.echo("  ── Summary ──")
    click.echo(f"    Name:               {name}")
    click.echo(f"    Email:              {email}")
    click.echo(f"    Role:               {role}")
    click.echo(f"    Company:            {company}")
    click.echo(f"    Territory:          {territory or '(not set)'}")
    click.echo(f"    Salesforce user ID: {salesforce_user_id or '(not set)'}")
    click.echo(f"    Accounts:           {', '.join(account_names) if account_names else '(none)'}")
    click.echo(f"    Data dir:           {data_dir}")
    click.echo(f"    OAuth:              {'configured' if oauth_id else 'skipped'}")
    click.echo(f"    ShadowBot ID:       {shadowbot_assistant_id or '(not set)'}")
    click.echo()
    try:
        confirm = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        click.echo()
        click.echo("  Cancelled.")
        return None
    if confirm in ("n", "no"):
        click.echo("  Cancelled.")
        return False
    return True


def _wizard_write_artifacts(
    name: str,
    email: str,
    role: str,
    company: str,
    territory: str,
    salesforce_user_id: str,
    account_names: list[str],
    data_dir: Path,
    oauth_id: str,
    oauth_secret: str,
    shadowbot_assistant_id: str = "",
    *,
    dry_run: bool = False,
) -> None:
    """Write all configuration artifacts to disk."""
    validate_dotenv_values({"GOOGLE_OAUTH_CLIENT_ID": oauth_id, "GOOGLE_OAUTH_CLIENT_SECRET": oauth_secret})
    data_dir = bind_initialization_workspace(data_dir)
    directories = ["config", "accounts", "data"]
    files = ["config/identity.yaml", "config/accounts.yaml"]
    for account_name in account_names:
        account_path = f"accounts/{account_key(account_name)}"
        directories.extend(
            [account_path, *(f"{account_path}/{sub}" for sub in ("pursuits", "meetings", "projects", "proposals"))]
        )
        files.append(f"{account_path}/account.md")
    if oauth_id or oauth_secret:
        files.append(".env")
    validate_initialization_paths(data_dir, tuple(directories), tuple(files))
    _load_existing_config()
    _load_existing_identity(data_dir)
    read_accounts_mapping_for_update(data_dir / "config" / "accounts.yaml")
    if dry_run:
        click.echo("Preview: initialization inputs and destinations validated; no files written.")
        return
    click.echo()
    click.echo("  Writing configuration…")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "accounts").mkdir(exist_ok=True)

    identity: dict[str, object] = {
        "role": role,
        "company": company,
        "name": name,
        "email": email,
        "territory": territory,
        "salesforce_user_id": salesforce_user_id,
        "accounts": account_names,
        "motions": ["Pre-sales pursuit", "Landed account expansion", "Relationship maintenance"],
    }
    _write_identity_yaml(data_dir, identity)
    _write_accounts_yaml(data_dir, account_names)
    for acct in account_names:
        _write_account_stub(data_dir, acct)
    _write_env(data_dir, oauth_id, oauth_secret)
    _wizard_write_config(data_dir, name, email, role, company, shadowbot_assistant_id)


def _wizard_write_config(
    data_dir: Path,
    name: str,
    email: str,
    role: str,
    company: str,
    shadowbot_assistant_id: str = "",
) -> None:
    """Write merged ~/.config/fieldkit/config.yaml, preserving unknown keys."""
    existing_raw = _load_existing_config()
    cfg.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldkit_root = discover_source_checkout(Path(__file__))
    config_data: dict[str, str] = {
        "fieldkit_home": str(data_dir),
        "pipeline_db": str(data_dir / "data" / "pipeline.db"),
        "gmail_db": str(data_dir / "data" / "gmail.db"),
    }
    if fieldkit_root is not None and "fieldkit_root" not in existing_raw:
        config_data["fieldkit_root"] = str(fieldkit_root)
    for key, val in (
        ("name", name),
        ("email", email),
        ("role", role),
        ("company", company),
    ):
        if val:
            config_data[key] = val

    merged: dict[str, object] = {**existing_raw, **config_data}
    if shadowbot_assistant_id:
        existing_shadowbot = merged.get("shadowbot")
        existing_shadowbot_dict: dict[str, object] = existing_shadowbot if isinstance(existing_shadowbot, dict) else {}
        merged["shadowbot"] = {**existing_shadowbot_dict, "assistant_id": shadowbot_assistant_id}
    atomic_yaml_write(Path(cfg.CONFIG_PATH), merged)
    click.echo(f"    ✓ {cfg.CONFIG_PATH}")


def _render_github_guidance() -> None:
    """Render the issue-board next step from the persisted GitHub configuration."""
    try:
        raw_config = read_config_mapping_for_update(cfg.CONFIG_PATH)
    except cfg.ConfigError:
        logger.debug("setup: failed to inspect GitHub configuration for next-step guidance")
        raw_config = {}
    raw_repo = raw_config.get("github_repo")
    github_repo = raw_repo.strip() if isinstance(raw_repo, str) else ""
    if github_repo:
        click.echo("    • Run 'fieldkit issue board' to see the open issue queue")
    else:
        click.echo("    • Add github_repo to config.yaml before using 'fieldkit issue' commands")


def _install_skills_after_setup() -> None:
    """Attempt the interactive post-setup skill installation with a bounded wait."""
    click.echo("  Installing fieldkit skills into the detected agent harness…")
    try:
        result = subprocess.run(
            ["fieldkit", "skill", "install"],
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_HEALTH_CHECK,
        )
        if result.returncode == 0:
            click.echo("    ✓ Skills installed")
        else:
            click.echo(f"    ⚠ skill install failed (non-fatal): {result.stderr.strip()[:80]}")
    except (OSError, subprocess.TimeoutExpired):
        click.echo(
            "    ⚠ fieldkit skill install unavailable on PATH — run 'fieldkit skill install' from an interactive terminal"
        )


def _defer_skill_install_after_setup() -> None:
    """Tell non-interactive setup callers how to perform the deferred action."""
    click.echo("    • Run 'fieldkit skill install' from an interactive terminal when ready")


def _wizard_post_setup(oauth_id: str, shadowbot_assistant_id: str = "", *, install_skills: bool = True) -> None:
    """Print next-steps and run post-setup tasks."""
    click.echo()
    click.echo("  ✓ Setup complete!")
    click.echo()
    click.echo("  Next steps:")
    click.echo("    • Run 'fieldkit doctor' to inspect your local configuration")
    click.echo("    • Run 'fieldkit skill list' to discover the bundled workflows (offline)")
    click.echo("    • Configure and authenticate optional integrations before running their workflows")
    if shadowbot_assistant_id:
        click.echo(
            "    • Add the ShadowBot OAuth endpoints and redirect URI to config.yaml, then run 'fieldkit auth shadowbot'"
        )
    _render_github_guidance()
    click.echo()
    (_install_skills_after_setup if install_skills else _defer_skill_install_after_setup)()
    if not oauth_id:
        click.echo("    • Add Google OAuth credentials when ready: 'fieldkit init' again")
    click.echo()


def _run_wizard(answers_path: Path | None = None, *, dry_run: bool = False) -> int:
    """Run interactive or answers-file setup. Returns exit code."""
    if dry_run and answers_path is None:
        raise cfg.ConfigError("Initialization preview requires an answers file or minimal workspace")
    answers = load_answers(answers_path) if answers_path is not None else None
    click.echo()
    click.echo("  ╔══════════════════════════════════════╗")
    click.echo("  ║   fieldkit — first-run setup wizard   ║")
    click.echo("  ╚══════════════════════════════════════╝")
    click.echo()
    if answers is None:
        click.echo("  Press Enter to accept the value shown in [brackets].")
        click.echo("  Ctrl-C to cancel without saving.")

    existing_cfg = _load_existing_config()
    if cfg.CONFIG_PATH.exists():
        click.echo()
        click.echo(f"  Existing config found: {cfg.CONFIG_PATH}")
        if answers is None:
            click.echo("  You can update individual values or press Enter to keep them.")

    inputs = answers if answers is not None else _wizard_prompt_inputs(existing_cfg)

    if answers is None:
        proceed = _wizard_confirm(
            inputs.name,
            inputs.email,
            inputs.role,
            inputs.company,
            inputs.territory,
            inputs.salesforce_user_id,
            list(inputs.account_names),
            inputs.data_dir,
            inputs.oauth_id,
            inputs.shadowbot_assistant_id,
        )
        if proceed is None:
            return 1
        if not proceed:
            return 0

    _wizard_write_artifacts(
        inputs.name,
        inputs.email,
        inputs.role,
        inputs.company,
        inputs.territory,
        inputs.salesforce_user_id,
        list(inputs.account_names),
        inputs.data_dir,
        inputs.oauth_id,
        inputs.oauth_secret,
        inputs.shadowbot_assistant_id,
        dry_run=dry_run,
    )
    if dry_run:
        return 0
    _wizard_post_setup(
        inputs.oauth_id,
        inputs.shadowbot_assistant_id,
        install_skills=answers is None,
    )
    return 0
