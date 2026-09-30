"""Canonical selection plan for optional aggregate-command integrations."""

from dataclasses import dataclass
from typing import Literal

from fieldkit.config import (
    ConfigError,
    McpEndpointName,
    get_integration_configuration_state,
    get_llm_model,
    get_mcp_endpoint,
    llm_disabled,
)


@dataclass(frozen=True)
class IntegrationPlan:
    """Services and optional watchers selected for one aggregate run."""

    preflight_services: tuple[str, ...]
    optional_watchers: tuple[str, ...]
    calendar: bool
    llm: bool
    skipped: tuple[str, ...]


def _configured_service(
    service: Literal["google", "sf"],
    *,
    selected: bool,
    required: bool,
    skipped: list[str],
) -> bool:
    label = "gmail" if service == "google" else "salesforce"
    if not selected:
        skipped.append(f"{label}: not selected")
        return False
    state = get_integration_configuration_state(service)
    if state == "invalid":
        raise ConfigError(f"Selected {service} integration configuration is invalid")
    if state == "disabled":
        if required:
            raise ConfigError(f"Selected {label} integration is not configured")
        skipped.append(f"{label}: not configured")
        return False
    return True


def _configured_endpoint(
    name: McpEndpointName,
    label: str,
    *,
    selected: bool,
    skipped: list[str],
) -> bool:
    if not selected:
        skipped.append(f"{label}: not selected")
        return False
    endpoint = get_mcp_endpoint(name)
    if endpoint is None:
        skipped.append(f"{label}: not configured")
        return False
    return True


def build_integration_plan(
    *,
    google_when_configured: bool,
    sf_requested: bool,
    backstory_when_configured: bool,
    draft_queue_when_configured: bool,
    calendar_when_configured: bool,
    slack_requested: bool,
    llm_when_configured: bool,
) -> IntegrationPlan:
    """Resolve selected services without contacting them or reading credentials."""
    skipped: list[str] = []
    preflight: list[str] = []
    optional_watchers: list[str] = []

    if _configured_service("google", selected=google_when_configured, required=False, skipped=skipped):
        preflight.append("gmail")
    if _configured_service("sf", selected=sf_requested, required=True, skipped=skipped):
        preflight.append("sf")

    if _configured_endpoint("backstory", "backstory-health", selected=backstory_when_configured, skipped=skipped):
        optional_watchers.append("backstory-health")
    if _configured_endpoint("draft_queue", "draft-queue", selected=draft_queue_when_configured, skipped=skipped):
        optional_watchers.append("draft-queue")

    if slack_requested:
        optional_watchers.append("slack-threads")
    else:
        skipped.append("slack-threads: not selected")

    calendar = _configured_endpoint("calendar", "calendar", selected=calendar_when_configured, skipped=skipped)

    llm = False
    if not llm_when_configured:
        skipped.append("llm: not selected")
    elif llm_disabled():
        skipped.append("llm: disabled by configuration")
    elif get_llm_model() is None:
        skipped.append("llm: not configured")
    else:
        llm = True
        preflight.append("llm")

    return IntegrationPlan(
        preflight_services=tuple(preflight),
        optional_watchers=tuple(optional_watchers),
        calendar=calendar,
        llm=llm,
        skipped=tuple(skipped),
    )


__all__ = ["IntegrationPlan", "build_integration_plan"]
