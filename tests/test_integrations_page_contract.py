"""Claims on the public integrations page that depend on runtime behavior."""

import tomllib
from pathlib import Path

import pytest

from fieldkit.__main__ import _COMMANDS
from fieldkit.config import GOOGLE_OAUTH_SCOPES
from fieldkit.config._integrations import get_mcp_endpoint
from fieldkit.config.optional_dependencies import OPTIONAL_PROFILE_IMPORT_ROOTS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs/integrations.md").read_text(encoding="utf-8")


def test_profile_installation_and_base_dispatch_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert "uv tool install 'fieldkit-cli[google]'" in PAGE
    assert "add `--force`" in PAGE
    assert project["name"] == "fieldkit-cli"
    assert set(extras) == {*OPTIONAL_PROFILE_IMPORT_ROOTS, "all"}
    assert "google-api-python-client>=2.131.0" in extras["google"]
    assert "litellm>=1.95,<2" in extras["llm"]
    assert set(extras["web"]) == {"fastapi>=0.111", "uvicorn>=0.30"}
    assert set(extras["chrome-auth"]) == {"cryptography>=50.0.0", "secretstorage>=3.0"}
    assert set(extras["all"]) == set().union(*(set(extras[name]) for name in OPTIONAL_PROFILE_IMPORT_ROOTS))
    assert "fieldkit's portable core has no mandatory SaaS dependency" in PAGE
    assert _COMMANDS["meeting"][1] == "fieldkit.commands.meeting.cli"


def test_google_profile_does_not_imply_credentials_or_narrow_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.config import resolve_oauth_credentials

    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert resolve_oauth_credentials() == (None, None)
    assert "installing it does not configure or authorize the separate workbook service" in PAGE
    assert "one\nshared scope set" in PAGE
    assert GOOGLE_OAUTH_SCOPES == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    assert "Gmail read-only, Google Docs read/write, and Google Drive\nread/write" in PAGE
    assert "configured `gmail_token` path overrides" in PAGE
    assert "`google-oauth-token.json` in the runtime-data root" in PAGE


def test_optional_mcp_routes_are_independent_and_missing_inputs_are_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config._integrations._loader._load_raw_config", lambda: {})
    assert get_mcp_endpoint("backstory") is None
    assert get_mcp_endpoint("calendar") is None
    assert get_mcp_endpoint("draft_queue") is None
    monkeypatch.setattr(
        "fieldkit.config._integrations._loader._load_raw_config",
        lambda: {"mcp_endpoints": {"backstory": "https://example.com/backstory"}},
    )
    assert get_mcp_endpoint("backstory") == "https://example.com/backstory"
    assert get_mcp_endpoint("calendar") is None
    assert get_mcp_endpoint("draft_queue") is None
    assert "each use an independent full URL under `mcp_endpoints`" in PAGE
    assert "fieldkit does\nnot ship a default route for them" in PAGE
    assert "Malformed calendar, Backstory, or\ndraft data is reported as unavailable or failed" in PAGE
    assert "fieldkit doctor google\nfieldkit doctor gmail" in PAGE
    assert "fieldkit doctor sf" in PAGE
