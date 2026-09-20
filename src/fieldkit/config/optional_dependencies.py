"""Narrow import-root checks for optional installation profiles."""

import importlib.util
import sys

from fieldkit.errors import MissingOptionalDependencyError

GOOGLE_IMPORT_ROOTS = (
    "google.auth",
    "google.oauth2",
    "google_auth_httplib2",
    "google_auth_oauthlib",
    "googleapiclient",
)
LLM_IMPORT_ROOTS = ("agentplatform", "litellm", "openai", "vertexai")
WEB_IMPORT_ROOTS = ("fastapi", "uvicorn")
CHROME_AUTH_IMPORT_ROOTS = ("cryptography", "secretstorage")
OPTIONAL_PROFILE_IMPORT_ROOTS = {
    "google": GOOGLE_IMPORT_ROOTS,
    "llm": LLM_IMPORT_ROOTS,
    "web": WEB_IMPORT_ROOTS,
    "chrome-auth": CHROME_AUTH_IMPORT_ROOTS,
}
SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY = "fieldkit.skip_optional_profile_checks"


def require_optional_profile(command: str, profile: str, import_roots: tuple[str, ...]) -> None:
    """Raise actionable guidance when a declared optional profile is incomplete."""
    missing: list[str] = []
    for import_root in import_roots:
        try:
            available = importlib.util.find_spec(import_root) is not None
        except ValueError:
            # Test doubles and embedded importers can provide a usable module
            # without a ModuleSpec. find_spec() raises ValueError only after
            # finding that module in the import cache.
            if sys.modules.get(import_root) is None:
                raise
            available = True
        except ModuleNotFoundError as exc:
            parts = import_root.split(".")
            expected_missing = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
            if exc.name not in expected_missing:
                raise
            available = False
        if not available:
            missing.append(import_root)
    if missing:
        raise MissingOptionalDependencyError(command, profile, tuple(missing))
