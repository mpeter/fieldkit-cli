"""Private configuration accessors for local settings and identity."""

import os

from fieldkit.config import _loader


def llm_disabled() -> bool:
    """Return whether either supported environment flag disables LLM calls."""
    return bool(os.environ.get("FIELDKIT_NO_LLM") or os.environ.get("NO_LLM"))


def get_driver_max_concurrent() -> int:
    """Return the configured driver concurrency cap, clamped to one through four."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return 1
    if data is None:
        return 1
    driver = data.get("driver")
    if not isinstance(driver, dict):
        return 1
    try:
        value = int(driver.get("max_concurrent", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(4, value))


def get_vertex_location() -> str | None:
    """Return the configured Vertex region when readable and nonempty."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return None
    if data is None:
        return None
    return str(data.get("vertex_location", "")).strip() or None


def get_llm_model() -> str | None:
    """Return the configured Vertex-routed LLM model, if any."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return None
    if data is None:
        return None
    value = data.get("llm_model", "")
    model = str(value).strip() if value else ""
    if not model:
        return None
    if not model.startswith("vertex_ai/"):
        raise _loader.ConfigError(
            f"llm_model config value {model!r} must start with 'vertex_ai/'. Example: vertex_ai/claude-sonnet-4-6"
        )
    return model


def get_user_email() -> str | None:
    """Return the configured user email, including the identity fallback."""
    data = _loader._load_raw_config()
    if data is None:
        return None
    email = data.get("email")
    if isinstance(email, str) and email:
        return email
    identity = data.get("identity")
    if isinstance(identity, dict):
        email = identity.get("email")
        if isinstance(email, str) and email:
            return email
    return None


def get_email_domain() -> str | None:
    """Return the configured email domain or derive it from the user email."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return None
    if data is None:
        return None
    value = data.get("email_domain", "")
    domain = str(value).strip() if value else ""
    if domain:
        return domain
    email = get_user_email()
    if email and "@" in email:
        return email.split("@", 1)[1]
    return None


def get_user_email_from_env() -> str | None:
    """Derive the user email from an explicit override or USER and config."""
    email = os.environ.get("FIELDKIT_USER_EMAIL", "").strip()
    if email:
        return email
    user = os.environ.get("USER", "").strip()
    if not user:
        return None
    domain = get_email_domain()
    return f"{user}@{domain}" if domain else None


def get_user_name() -> str:
    """Return the configured user display name, or an empty string."""
    raw = _loader._load_raw_config()
    return str((raw or {}).get("name", "")).strip()


def get_github_repo() -> str:
    """Return the required configured GitHub repository slug."""
    data = _loader._load_raw_config()
    if data is None:
        raise _loader.ConfigError(f"Config file not found or unreadable: {_loader.CONFIG_PATH}")
    if "github_repo" not in data:
        raise _loader.ConfigError(
            f"Config file {_loader.CONFIG_PATH} is missing required key 'github_repo'."
            " Add: github_repo: owner/repo  (e.g. owner/fieldkit-project)"
        )
    value = str(data["github_repo"]).strip()
    if not value:
        raise _loader.ConfigError("Config key 'github_repo' must not be empty or whitespace")
    return value


def get_companion_tier() -> str:
    """Return the companion permission tier, failing closed to ``read``."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return "read"
    if data is None:
        return "read"
    companion = data.get("companion")
    if not isinstance(companion, dict):
        return "read"
    tier = str(companion.get("tier", "read")).strip().lower()
    return tier if tier in ("read", "propose", "act") else "read"


def get_companion_act_allowlist() -> list[str]:
    """Return the configured companion act-tier allowlist, or an empty list."""
    try:
        data = _loader._load_raw_config()
    except _loader.ConfigError:
        return []
    if data is None:
        return []
    companion = data.get("companion")
    if not isinstance(companion, dict):
        return []
    raw = companion.get("act_allowlist", [])
    if not isinstance(raw, list):
        return []
    return [str(entry).strip() for entry in raw if str(entry).strip()]
