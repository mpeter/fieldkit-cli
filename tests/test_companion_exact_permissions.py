"""Companion approval binds every argument, not just a command prefix."""

import pytest

from fieldkit.commands.companion.cli import _act_policy_validation
from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "argv",
    [
        ["pursuit", "advance", "other/deal", "--dry-run"],
        ["pursuit", "advance", "acme/deal", "--dry-run", "--force"],
        ["pursuit", "advance", "acme/deal", "--dry-run", "--output", "other.md"],
        ["pursuit", "advance", "acme/deal"],
    ],
)
def test_act_approval_denies_changed_or_added_arguments(argv: list[str]) -> None:
    validation = _act_policy_validation("act", ["pursuit advance acme/deal --dry-run"])
    assert validation.policy is not None
    result = is_allowed(argv, "act", validation.policy)

    assert result is False


def test_prefix_entry_does_not_approve_an_unspecified_target() -> None:
    result = is_allowed(["pursuit", "advance", "acme/deal", "--dry-run"], "act", NO_ACT_POLICY)

    assert result is False


def test_quoted_argument_is_one_exact_token() -> None:
    validation = _act_policy_validation("act", ["pursuit advance 'acme/deal name' --dry-run"])
    assert validation.policy is not None
    result = is_allowed(["pursuit", "advance", "acme/deal name", "--dry-run"], "act", validation.policy)

    assert result is True


def test_unclosed_allowlist_quote_is_invalid_and_denied() -> None:
    entry = "pursuit advance 'acme/deal --dry-run"
    result = _act_policy_validation("act", [entry])

    assert result.problems
    assert is_allowed(["pursuit", "advance", "acme/deal", "--dry-run"], "act", NO_ACT_POLICY) is False


def test_configuration_cannot_invent_preview_support() -> None:
    result = _act_policy_validation("act", ["doctor gmail --dry-run"])

    assert result.policy is None
    assert result.problems == ("allowlist entry 'doctor gmail --dry-run': command does not support --dry-run",)
