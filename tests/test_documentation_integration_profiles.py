"""Semantic ownership of the published integration installation-profile table."""

import json
from pathlib import Path

import pytest

from fieldkit.config import optional_dependencies, resolve_oauth_credentials
from fieldkit.errors import MissingOptionalDependencyError
from scripts.check_dependency_profiles import validate
from scripts.markdown_tables import MarkdownTable, markdown_tables, parse_markdown_tables

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parent.parent


def _assert_optional_profile_prose(document: str) -> None:
    section = document.split("## Add context deliberately\n", 1)[1].split("\n## ", 1)[0]
    paragraphs = {" ".join(paragraph.split()) for paragraph in section.split("\n\n")}
    assert (
        "1. install any required optional dependency profile; and "
        "2. configure credentials and endpoints you are authorized to use."
    ) in paragraphs


def test_user_guide_rejects_prefixed_optional_profile_negation() -> None:
    document = (_ROOT / "docs/user-guide.md").read_text(encoding="utf-8")
    before = "1. install any required optional dependency profile; and"
    assert before in document
    with pytest.raises(AssertionError):
        _assert_optional_profile_prose(document.replace(before, "It is false that " + before))


@pytest.mark.parametrize(
    "replacement",
    [
        "install its optional dependency profile",
        "install every optional dependency profile",
        "do not install any required optional dependency profile",
    ],
)
def test_user_guide_rejects_universal_optional_profile_requirement(replacement: str) -> None:
    document = (_ROOT / "docs/user-guide.md").read_text(encoding="utf-8")
    before = "install any required optional dependency profile"
    assert before in document
    with pytest.raises(AssertionError):
        _assert_optional_profile_prose(document.replace(before, replacement))


def _assert_profile_table(table: MarkdownTable) -> None:
    assert table.header == ("Profile", "Adds", "Configuration or access still required")
    profiles = json.loads((_ROOT / "docs/release-readiness/dependency-ownership.json").read_text(encoding="utf-8"))[
        "profiles"
    ]
    rows = {row[0].strip("`"): row[1:] for row in table.rows}
    assert len(rows) == len(table.rows), "Duplicate profile"
    assert set(rows) == set(profiles), "Profile inventory differs from dependency ownership"
    # Exact approved policy cells reject negated claims that preserve the same
    # keywords. The dependency and runtime checks below establish their behavior.
    approved = {
        "base": ("Local CLI, workspace, pursuit, task, and skill behavior", "None for minimal first success"),
        "google": ("Google API clients and OAuth support", "Your OAuth client and consent for the APIs you enable"),
        "llm": (
            "LiteLLM and supported provider clients",
            "Provider project/account, credentials, region, and model access",
        ),
        "web": ("FastAPI and Uvicorn", "Local web configuration; no hosted fieldkit service exists"),
        "chrome-auth": (
            "Browser credential-store dependencies",
            "Supported Linux desktop, local browser profile, and authorized session",
        ),
        "all": ("Every dependency above", "Every service is still configured independently"),
    }
    for profile, cells in rows.items():
        assert cells == approved[profile], f"{profile}: unapproved capability/access semantics"
    assert {"google-api-python-client", "google-auth-oauthlib"} <= set(profiles["google"]["requirements"])
    assert "litellm" in profiles["llm"]["requirements"]
    assert set(profiles["web"]["requirements"]) == {"fastapi", "uvicorn"}
    assert set(profiles["chrome-auth"]["requirements"]) == {"cryptography", "secretstorage"}
    assert set(profiles["all"]["composes"]) == set(rows) - {"base", "all"}


def test_integration_profile_table_matches_installation_and_import_contract() -> None:
    _assert_optional_profile_prose((_ROOT / "docs/user-guide.md").read_text(encoding="utf-8"))
    report = validate(_ROOT)
    assert report.ok, report.findings
    tables = markdown_tables(_ROOT / "docs/integrations.md")
    assert len(tables) == 1
    _assert_profile_table(tables[0])
    profiles = json.loads((_ROOT / "docs/release-readiness/dependency-ownership.json").read_text(encoding="utf-8"))[
        "profiles"
    ]
    assert set(optional_dependencies.OPTIONAL_PROFILE_IMPORT_ROOTS) == set(profiles) - {"base", "all"}
    for profile, roots in optional_dependencies.OPTIONAL_PROFILE_IMPORT_ROOTS.items():
        assert set(roots) == set(profiles[profile]["import_roots"])


@pytest.mark.parametrize("profile", ["google", "llm", "web", "chrome-auth"])
def test_integration_profile_requires_libraries_before_service_access(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    roots = optional_dependencies.OPTIONAL_PROFILE_IMPORT_ROOTS[profile]
    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", lambda _name: None)
    with pytest.raises(MissingOptionalDependencyError, match=profile):
        optional_dependencies.require_optional_profile("documentation probe", profile, roots)
    monkeypatch.setattr("fieldkit.config.optional_dependencies.importlib.util.find_spec", lambda _name: object())
    result = optional_dependencies.require_optional_profile("documentation probe", profile, roots)
    assert result is None


def test_google_library_installation_does_not_supply_oauth_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    result = resolve_oauth_credentials()
    assert result == (None, None)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("| base |", "| salesforce |"),
        ("Google API clients and OAuth support", "Google API clients"),
        ("Google API clients and OAuth support", "No Google API clients and OAuth support"),
        (
            "Your OAuth client and consent for the APIs you enable",
            "No OAuth client or consent for the APIs you enable is required",
        ),
        ("Provider project/account, credentials, region, and model access", "Provider model access"),
        (
            "Provider project/account, credentials, region, and model access",
            "No provider project/account, credentials, region, and model access is required",
        ),
        ("Local web configuration; no hosted fieldkit service exists", "Hosted fieldkit service"),
        ("Supported Linux desktop, local browser profile, and authorized session", "Any browser session"),
        ("Every service is still configured independently", "Every service is configured automatically"),
        ("Every service is still configured independently", "Not every service is still configured independently"),
    ],
)
def test_integration_table_rejects_false_profile_or_access_claims(before: str, after: str) -> None:
    source = markdown_tables(_ROOT / "docs/integrations.md")[0].source_text
    assert before in source
    tables = parse_markdown_tables(source.replace(before, after))
    assert len(tables) == 1
    with pytest.raises(AssertionError, match=r"inventory|semantics"):
        _assert_profile_table(tables[0])
