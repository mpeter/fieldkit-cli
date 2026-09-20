"""Tests for fieldkit.commands.sf.opportunity._fetch_contract_type (historic regression).

Local, self-contained: does not import from tests/test_sf_opportunity.py.

``_fetch_contract_type`` opens its own ``SFDirectClient`` (mirroring
``_fetch_deal_splits``) and, per the conftest ``_block_outbound_network``
docstring, has a documented history of reaching the real Salesforce API
when a session is configured but the client/``opp_contract_type`` call
isn't mocked. Every test here mocks ``SFDirectClient`` and
``opp_contract_type`` fully; the network guard fixture is a safety net,
not something these tests rely on.
"""

from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.sf.opportunity import _fetch_contract_type

pytestmark = pytest.mark.unit

_OPP_ID = "006FAKE00000001AAA"
_SID = "fake-session-id"
_BASE_URL = "https://example-org.my.salesforce.com"


def _make_mock_client() -> MagicMock:
    """Build a mock SFDirectClient class whose instances are real context managers.

    The returned MagicMock stands in for the ``SFDirectClient`` class itself:
    calling it returns an instance mock whose ``__enter__`` returns the
    "entered" client object that ``opp_contract_type`` should receive.
    """
    client_cls = MagicMock(name="SFDirectClient")
    instance = client_cls.return_value
    entered = MagicMock(name="entered_client")
    instance.__enter__.return_value = entered
    instance.__exit__.return_value = None
    return client_cls


# ── Behavior 1: no session id → "standard", nothing else touched ──────────────


def test_no_session_id_returns_standard_without_further_calls() -> None:
    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=None) as mock_get_sid,
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url") as mock_get_base_url,
        patch("fieldkit.sf.client.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.opportunity.opp_contract_type") as mock_opp_contract_type,
    ):
        result = _fetch_contract_type(_OPP_ID)

    assert result == "standard"
    mock_get_sid.assert_called_once()
    mock_get_base_url.assert_not_called()
    mock_client_cls.assert_not_called()
    mock_opp_contract_type.assert_not_called()


def test_empty_string_session_id_also_returns_standard() -> None:
    # Falsy but not None — the guard is `if not sid`, not `if sid is None`.
    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=""),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url") as mock_get_base_url,
        patch("fieldkit.sf.client.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.opportunity.opp_contract_type") as mock_opp_contract_type,
    ):
        result = _fetch_contract_type(_OPP_ID)

    assert result == "standard"
    mock_get_base_url.assert_not_called()
    mock_client_cls.assert_not_called()
    mock_opp_contract_type.assert_not_called()


# ── Behavior 2: session id present, no base url → "standard", client untouched ─


def test_session_id_but_no_base_url_returns_standard_without_client() -> None:
    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=_SID),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value=None) as mock_get_base_url,
        patch("fieldkit.sf.client.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.opportunity.opp_contract_type") as mock_opp_contract_type,
    ):
        result = _fetch_contract_type(_OPP_ID)

    assert result == "standard"
    mock_get_base_url.assert_called_once()
    mock_client_cls.assert_not_called()
    mock_opp_contract_type.assert_not_called()


# ── Behavior 3 & 4: both present → client constructed with exact kwargs, ──────
# ── entered, and opp_contract_type called with the entered client + opp_id ────


def test_session_and_base_url_present_constructs_client_and_returns_opp_contract_type_result() -> None:
    mock_client_cls = _make_mock_client()
    instance = mock_client_cls.return_value
    entered = instance.__enter__.return_value

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=_SID),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value=_BASE_URL),
        patch("fieldkit.sf.client.SFDirectClient", mock_client_cls),
        patch(
            "fieldkit.commands.sf.opportunity.opp_contract_type",
            return_value="fixed_price",
        ) as mock_opp_contract_type,
    ):
        result = _fetch_contract_type(_OPP_ID)

    # Client constructed with exactly session_id and base_url as kwargs.
    mock_client_cls.assert_called_once_with(session_id=_SID, base_url=_BASE_URL)

    # The `with` block genuinely entered/exited the context manager.
    instance.__enter__.assert_called_once()
    instance.__exit__.assert_called_once()

    # opp_contract_type called with the ENTERED client (not the raw instance)
    # and the original opp_id.
    mock_opp_contract_type.assert_called_once_with(entered, _OPP_ID)

    # The function's return value IS opp_contract_type's return value.
    assert result == "fixed_price"
