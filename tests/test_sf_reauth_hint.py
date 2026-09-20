"""Unit tests for fieldkit.sf.client.reauth_hint_message() (task 5.3)."""

from unittest.mock import patch

import pytest

from fieldkit.sf.client import reauth_hint_message

pytestmark = pytest.mark.unit

_MODULE = "fieldkit.sf.client"


def test_reauth_hint_message_with_configured_url() -> None:
    """When SF org URL is configured, the hostname appears in the hint."""
    with patch(f"{_MODULE}.get_salesforce_org_url", return_value="https://myorg.my.salesforce.com"):
        result = reauth_hint_message()
    assert "myorg.my.salesforce.com" in result
    assert "auth sf" in result


def test_reauth_hint_message_unconfigured_returns_generic() -> None:
    """When SF org URL is empty, the hint uses generic phrasing."""
    with patch(f"{_MODULE}.get_salesforce_org_url", return_value=""):
        result = reauth_hint_message()
    assert "your Salesforce org" in result
    assert "auth sf" in result


def test_reauth_hint_message_does_not_raise_when_unconfigured() -> None:
    """reauth_hint_message() must not raise under any config state."""
    with patch(f"{_MODULE}.get_salesforce_org_url", return_value=""):
        result = reauth_hint_message()
    assert isinstance(result, str)


def test_reauth_hint_message_strips_trailing_slash() -> None:
    """Trailing slashes in the org URL are stripped from the hostname."""
    with patch(f"{_MODULE}.get_salesforce_org_url", return_value="https://myorg.my.salesforce.com/"):
        result = reauth_hint_message()
    assert "myorg.my.salesforce.com" in result
    assert result.count("myorg.my.salesforce.com") == 1
