"""Selection contracts for optional aggregate-command integrations."""

from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError
from fieldkit.watch.integration_plan import IntegrationPlan, build_integration_plan

pytestmark = pytest.mark.unit


def _plan(**overrides: bool) -> IntegrationPlan:
    options = {
        "google_when_configured": True,
        "sf_requested": False,
        "backstory_when_configured": True,
        "draft_queue_when_configured": False,
        "calendar_when_configured": False,
        "slack_requested": False,
        "llm_when_configured": False,
    }
    options.update(overrides)
    with (
        patch("fieldkit.watch.integration_plan.get_integration_configuration_state", return_value="disabled"),
        patch("fieldkit.watch.integration_plan.get_mcp_endpoint", return_value=None),
        patch("fieldkit.watch.integration_plan.get_llm_model", return_value=None),
        patch("fieldkit.watch.integration_plan.llm_disabled", return_value=False),
    ):
        return build_integration_plan(**options)


def test_base_plan_selects_only_local_work() -> None:
    plan = _plan()

    assert plan.preflight_services == ()
    assert plan.optional_watchers == ()
    assert plan.calendar is False
    assert plan.llm is False
    assert plan.skipped == (
        "gmail: not configured",
        "salesforce: not selected",
        "backstory-health: not configured",
        "draft-queue: not selected",
        "slack-threads: not selected",
        "calendar: not selected",
        "llm: not selected",
    )


def test_selected_services_and_watchers_have_one_ordered_plan() -> None:
    endpoints = {
        "backstory": "https://gateway.example.com/backstory",
        "draft_queue": "https://gateway.example.com/drafts",
        "calendar": "https://gateway.example.com/calendar",
    }
    with (
        patch("fieldkit.watch.integration_plan.get_integration_configuration_state", return_value="enabled"),
        patch("fieldkit.watch.integration_plan.get_mcp_endpoint", side_effect=endpoints.get),
        patch("fieldkit.watch.integration_plan.get_llm_model", return_value="vertex_ai/example-model"),
        patch("fieldkit.watch.integration_plan.llm_disabled", return_value=False),
    ):
        plan = build_integration_plan(
            google_when_configured=True,
            sf_requested=True,
            backstory_when_configured=True,
            draft_queue_when_configured=True,
            calendar_when_configured=True,
            slack_requested=True,
            llm_when_configured=True,
        )

    assert plan.preflight_services == ("gmail", "sf", "llm")
    assert plan.optional_watchers == ("backstory-health", "draft-queue", "slack-threads")
    assert plan.calendar is True
    assert plan.llm is True
    assert plan.skipped == ()


def test_disabled_llm_wins_over_configured_model() -> None:
    with (
        patch("fieldkit.watch.integration_plan.get_integration_configuration_state", return_value="disabled"),
        patch("fieldkit.watch.integration_plan.get_mcp_endpoint", return_value=None),
        patch("fieldkit.watch.integration_plan.get_llm_model", return_value="vertex_ai/example-model"),
        patch("fieldkit.watch.integration_plan.llm_disabled", return_value=True),
    ):
        plan = build_integration_plan(
            google_when_configured=False,
            sf_requested=False,
            backstory_when_configured=False,
            draft_queue_when_configured=False,
            calendar_when_configured=False,
            slack_requested=False,
            llm_when_configured=True,
        )

    assert plan.llm is False
    assert plan.preflight_services == ()
    assert "llm: disabled by configuration" in plan.skipped


@pytest.mark.parametrize("service", ["google", "sf"])
def test_invalid_selected_service_is_configuration_error(service: str) -> None:
    def _state(selected: str) -> str:
        return "invalid" if selected == service else "disabled"

    with (
        patch("fieldkit.watch.integration_plan.get_integration_configuration_state", side_effect=_state),
        patch("fieldkit.watch.integration_plan.get_mcp_endpoint", return_value=None),
        patch("fieldkit.watch.integration_plan.get_llm_model", return_value=None),
        patch("fieldkit.watch.integration_plan.llm_disabled", return_value=False),
        pytest.raises(ConfigError, match=service),
    ):
        build_integration_plan(
            google_when_configured=service == "google",
            sf_requested=service == "sf",
            backstory_when_configured=False,
            draft_queue_when_configured=False,
            calendar_when_configured=False,
            slack_requested=False,
            llm_when_configured=False,
        )


def test_requested_salesforce_must_be_configured() -> None:
    with (
        patch("fieldkit.watch.integration_plan.get_integration_configuration_state", return_value="disabled"),
        patch("fieldkit.watch.integration_plan.get_mcp_endpoint", return_value=None),
        patch("fieldkit.watch.integration_plan.get_llm_model", return_value=None),
        patch("fieldkit.watch.integration_plan.llm_disabled", return_value=False),
        pytest.raises(ConfigError, match=r"salesforce.*not configured"),
    ):
        build_integration_plan(
            google_when_configured=False,
            sf_requested=True,
            backstory_when_configured=False,
            draft_queue_when_configured=False,
            calendar_when_configured=False,
            slack_requested=False,
            llm_when_configured=False,
        )
