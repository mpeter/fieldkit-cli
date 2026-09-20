"""Private Pydantic schemas for validating fieldkit configuration."""

from pydantic import BaseModel, ConfigDict


class _ShadowbotConfig(BaseModel):
    """Schema for the optional ``shadowbot:`` section in config.yaml."""

    model_config = ConfigDict(extra="ignore")
    api_base: str | None = None
    token_endpoint: str | None = None
    auth_endpoint: str | None = None
    client_id: str | None = None
    assistant_id: str | None = None
    chrome_cookies_path: str | None = None


class _FieldkitConfig(BaseModel):
    """Internal schema for config.yaml validation.

    Required-field enforcement remains in individual accessors. Unknown keys are
    ignored so operator-specific configuration extensions remain valid.
    """

    model_config = ConfigDict(extra="ignore")
    fieldkit_home: str | None = None
    fieldkit_data: str | None = None
    fieldkit_root: str | None = None
    data_repo: str | None = None
    email: str | None = None
    email_domain: str | None = None
    vertex_location: str | None = None
    github_repo: str | None = None
    google_token_path: str | None = None
    gmail_token: str | None = None
    sf_org_url: str | None = None
    shadowbot: _ShadowbotConfig | None = None
    accounts: dict[str, object] | None = None
    pipeline_quota: dict[str, object] | None = None
    territory_accounts: dict[str, str] | None = None
    mcp_gateway_url: str | None = None
