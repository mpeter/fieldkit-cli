"""Typed input contract for interactive and unattended initialization."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from fieldkit.config import ConfigError

DEFAULT_ROLE = "Account Engineer"
DEFAULT_COMPANY = ""
SHADOWBOT_ASSISTANT_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,128}$")
_ACCOUNT_KEY_RE = re.compile(r"^[a-z0-9_-]+$")

_ANSWERS_KEYS = frozenset(
    {
        "role",
        "company",
        "name",
        "email",
        "territory",
        "salesforce_user_id",
        "data_dir",
        "accounts",
        "gcp_project",
        "oauth_client_id",
        "oauth_client_secret",
        "shadowbot_assistant_id",
    }
)


@dataclass(frozen=True)
class InitInputs:
    """Validated values consumed by unattended fieldkit initialization."""

    role: str
    company: str
    name: str
    email: str
    territory: str
    salesforce_user_id: str
    data_dir: Path
    account_names: tuple[str, ...]
    gcp_project: str
    oauth_id: str
    oauth_secret: str
    shadowbot_assistant_id: str


def _required_answer(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Init answers key '{key}' must be a non-empty string")
    return value.strip()


def _optional_answer(data: dict[str, object], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"Init answers key '{key}' must be a string")
    return value.strip()


def account_key(name: str) -> str:
    """Return a safe workspace key for a human-readable account name."""
    key = name.strip().lower().replace(" ", "-")
    if not _ACCOUNT_KEY_RE.fullmatch(key):
        raise ConfigError(f"Account name cannot be converted to a safe slug: {name!r}")
    return key


def load_answers(path: Path) -> InitInputs:
    """Parse and validate an unattended-init YAML document."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read init answers from {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("Init answers document must contain a YAML mapping")
    if any(not isinstance(key, str) for key in loaded):
        raise ConfigError("Init answers keys must be strings")

    data: dict[str, object] = loaded
    unknown = sorted(set(data) - _ANSWERS_KEYS)
    if unknown:
        raise ConfigError(f"Unknown init answers key(s): {', '.join(unknown)}")

    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ConfigError("Init answers key 'accounts' must be a list of non-empty strings")
    if any(not isinstance(account, str) or not account.strip() for account in raw_accounts):
        raise ConfigError("Init answers key 'accounts' must be a list of non-empty strings")
    account_names = tuple(account.strip() for account in raw_accounts)
    for account in account_names:
        account_key(account)

    oauth_id = _optional_answer(data, "oauth_client_id")
    oauth_secret = _optional_answer(data, "oauth_client_secret")
    if oauth_secret and not oauth_id:
        raise ConfigError("Init answers oauth_client_secret requires oauth_client_id")

    shadowbot_assistant_id = _optional_answer(data, "shadowbot_assistant_id")
    if shadowbot_assistant_id and not SHADOWBOT_ASSISTANT_ID_RE.fullmatch(shadowbot_assistant_id):
        raise ConfigError("ShadowBot assistant ID contains invalid characters")

    return InitInputs(
        role=_optional_answer(data, "role", DEFAULT_ROLE),
        company=_optional_answer(data, "company", DEFAULT_COMPANY),
        name=_required_answer(data, "name"),
        email=_required_answer(data, "email"),
        territory=_optional_answer(data, "territory"),
        salesforce_user_id=_optional_answer(data, "salesforce_user_id"),
        data_dir=Path(_required_answer(data, "data_dir")).expanduser().resolve(),
        account_names=account_names,
        gcp_project=_optional_answer(data, "gcp_project"),
        oauth_id=oauth_id,
        oauth_secret=oauth_secret,
        shadowbot_assistant_id=shadowbot_assistant_id,
    )
