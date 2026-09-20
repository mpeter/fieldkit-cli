"""Unit tests for ``_fetch_territory_developer_name`` in ``fieldkit.sf.territory``.

CRAP cleanup (Track B): the function had 0% line coverage. ``client`` is only
touched through its private surface (``_base_url``, ``_request_with_retry``,
``_auth_headers``), so a bare ``MagicMock`` double stands in for
``SFDirectClient`` rather than constructing a real one. ``_parse_json_response``
is patched at ``fieldkit.sf.territory._parse_json_response`` (this module's
import binding) so each test can drive its return/raise behavior directly
without needing to fabricate a response shape it would actually parse.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.sf.client import SFAPIError
from fieldkit.sf.territory import _API_VERSION, _fetch_territory_developer_name

pytestmark = pytest.mark.unit

_BASE_URL = "https://examplecrm.my.salesforce.com"
_TERRITORY_ID = "00BFAKE00000001AAA"
_EXPECTED_URL = f"{_BASE_URL}/services/data/{_API_VERSION}/sobjects/Territory2/{_TERRITORY_ID}"
_AUTH_HEADERS = {"Authorization": "Bearer fake-sid"}


def _make_client(*, request_return: Any = None, request_side_effect: BaseException | None = None) -> MagicMock:
    """Build a double for SFDirectClient exposing only the private attrs the function touches."""
    client = MagicMock()
    client._base_url = _BASE_URL
    client._auth_headers.return_value = _AUTH_HEADERS
    if request_side_effect is not None:
        client._request_with_retry.side_effect = request_side_effect
    else:
        client._request_with_retry.return_value = request_return
    return client


def _make_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def test_request_error_returns_none_but_still_attempted_correct_url() -> None:
    """httpx.RequestError -> None, logged not raised; the call itself was still attempted correctly."""
    client = _make_client(request_side_effect=httpx.RequestError("connection reset"))

    result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result is None
    call = client._request_with_retry.call_args
    assert call.args == ("GET", _EXPECTED_URL)


def test_non_200_status_returns_none_without_parsing_json() -> None:
    """A non-200 status short-circuits before _parse_json_response is ever called."""
    client = _make_client(request_return=_make_response(status_code=404))

    with patch("fieldkit.sf.territory._parse_json_response") as mock_parse:
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result is None
    mock_parse.assert_not_called()


def test_parse_json_response_raising_sfapierror_returns_none() -> None:
    """A malformed/unexpected body (SFAPIError from _parse_json_response) -> None, not raised."""
    client = _make_client(request_return=_make_response(status_code=200))

    with patch("fieldkit.sf.territory._parse_json_response", side_effect=SFAPIError("bad body")):
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result is None


def test_developer_name_present_as_string_returns_it() -> None:
    """A 200 response with a string DeveloperName returns that string exactly."""
    client = _make_client(request_return=_make_response(status_code=200))

    with patch("fieldkit.sf.territory._parse_json_response", return_value={"DeveloperName": "NA_West"}):
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result == "NA_West"


def test_developer_name_key_absent_returns_none() -> None:
    """No 'DeveloperName' key at all -> dict.get default of None -> function returns None."""
    client = _make_client(request_return=_make_response(status_code=200))

    with patch("fieldkit.sf.territory._parse_json_response", return_value={"Id": _TERRITORY_ID}):
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result is None


@pytest.mark.parametrize("bad_value", [None, 42, ["NA_West"]])
def test_developer_name_present_but_not_a_string_returns_none(bad_value: Any) -> None:
    """A present-but-non-string DeveloperName -> None via the isinstance guard, not the .get default."""
    client = _make_client(request_return=_make_response(status_code=200))

    with patch("fieldkit.sf.territory._parse_json_response", return_value={"DeveloperName": bad_value}):
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result is None


def test_request_with_retry_called_with_full_expected_signature() -> None:
    """Happy path: assert the complete _request_with_retry call — method, url, headers, params."""
    client = _make_client(request_return=_make_response(status_code=200))

    with patch("fieldkit.sf.territory._parse_json_response", return_value={"DeveloperName": "NA_West"}):
        result = _fetch_territory_developer_name(client, _TERRITORY_ID)

    assert result == "NA_West"
    client._request_with_retry.assert_called_once_with(
        "GET", _EXPECTED_URL, headers=_AUTH_HEADERS, params={"fields": "DeveloperName"}
    )
