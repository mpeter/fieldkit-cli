"""Tests for fieldkit.commands.sf.session_check."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.commands.sf.session_check import check_sf_session

pytestmark = pytest.mark.unit

_COOKIE_DATA = {
    "cookies": [
        {"name": "sid", "value": "abc123", "domain": ".examplecrm.my.salesforce.com"},
        {"name": "other", "value": "xyz", "domain": ".examplecrm.my.salesforce.com"},
    ]
}


def _mock_cookie_file(tmp_path: Path, data: dict | None = None) -> Path:
    p = tmp_path / "sf-cookies.json"
    p.write_text(json.dumps(data if data is not None else _COOKIE_DATA), encoding="utf-8")
    return p


def _make_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def test_session_active(tmp_path: Path) -> None:
    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("httpx.get", return_value=_make_response(200)),
    ):
        alive, msg = check_sf_session()
    assert alive is True
    assert "instance:" in msg  # historic regression: return value is now "instance: {domain}"


def test_session_expired_401(tmp_path: Path) -> None:
    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("httpx.get", return_value=_make_response(401)),
    ):
        alive, msg = check_sf_session()
    assert alive is False
    assert "expired" in msg.lower()


def test_cookie_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.json"
    with patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=missing):
        alive, msg = check_sf_session()
    assert alive is False
    assert "not found" in msg.lower()


def test_network_error_retry_succeeds(tmp_path: Path) -> None:
    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch(
            "httpx.get",
            side_effect=[httpx.ConnectError("fail"), _make_response(200)],
        ),
        patch("fieldkit.commands.sf.session_check.time.sleep") as mock_sleep,
    ):
        alive, _msg = check_sf_session()
    assert alive is True
    mock_sleep.assert_called_once_with(1)


def test_network_error_retry_exhausted(tmp_path: Path) -> None:
    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch(
            "httpx.get",
            side_effect=[httpx.ConnectError("fail"), httpx.ConnectError("fail2")],
        ),
        patch("fieldkit.commands.sf.session_check.time.sleep"),
    ):
        alive, msg = check_sf_session()
    assert alive is False
    assert "Network error" in msg


def test_no_sid_cookie(tmp_path: Path) -> None:
    data = {"cookies": [{"name": "other", "value": "val", "domain": ".example.com"}]}
    cookie_file = _mock_cookie_file(tmp_path, data)
    with patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file):
        alive, msg = check_sf_session()
    assert alive is False
    assert "sid" in msg.lower()


# ---------------------------------------------------------------------------
# historic regression regression: session_check MUST use Bearer auth, not Cookie auth
# ---------------------------------------------------------------------------


# ── TestBearerAuthRegression (flattened) ────────────────────────────────────


def test_bearer_auth_regression_uses_bearer_auth_header(tmp_path: Path) -> None:
    """Probe request MUST include Authorization: Bearer {sid}, not a cookies kwarg."""
    cookie_file = _mock_cookie_file(tmp_path)
    captured: list[dict] = []

    def capture_get(url: str, **kwargs: object) -> MagicMock:
        captured.append(kwargs)
        return _make_response(200)

    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("httpx.get", side_effect=capture_get),
    ):
        alive, _ = check_sf_session()

    assert alive is True
    assert len(captured) == 1
    kw = captured[0]
    assert "headers" in kw, "Expected headers kwarg"
    assert "Authorization" in kw["headers"], "Expected Authorization header"
    assert kw["headers"]["Authorization"].startswith("Bearer "), "Must be Bearer token (historic regression)"
    assert "cookies" not in kw, "Must NOT pass cookies kwarg (historic regression)"


def test_bearer_auth_regression_rejects_non_my_salesforce_domain(tmp_path: Path) -> None:
    """sid from login.salesforce.com must be rejected; only *.my.salesforce.com allowed."""
    data = {"cookies": [{"name": "sid", "value": "abc", "domain": ".login.salesforce.com"}]}
    cookie_file = _mock_cookie_file(tmp_path, data)
    with patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file):
        alive, msg = check_sf_session()
    assert alive is False
    assert "sid" in msg.lower()


# ---------------------------------------------------------------------------
# historic regression regression: probe MUST hit an object-level endpoint, not just
# GET /services/data/ (which passed even when real object-level calls fail
# with 401 INVALID_SESSION_ID -- confirmed live 2026-08-15).
# ---------------------------------------------------------------------------


def test_bug_1453_probe_hits_object_level_describe_endpoint(tmp_path: Path) -> None:
    """The probe URL must be a real sobjects endpoint, not the bare /services/data/ listing."""
    cookie_file = _mock_cookie_file(tmp_path)
    captured_urls: list[str] = []

    def capture_get(url: str, **_kwargs: object) -> MagicMock:
        captured_urls.append(url)
        return _make_response(200)

    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("httpx.get", side_effect=capture_get),
    ):
        alive, _msg = check_sf_session()

    assert alive is True
    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert url.rstrip("/") != "https://examplecrm.my.salesforce.com/services/data", (
        "historic regression: probing the bare /services/data/ listing does not prove object-level access works"
    )
    assert "/sobjects/" in url, "Probe must hit a real sobject endpoint"
    assert "describe" in url, "Probe must hit describe (metadata-only, no data rows)"
    assert "/sobjects/Account/" in url, (
        "Probe object must be Account, not Organization -- Organization describe "
        "can return 404 for otherwise healthy restricted sessions, which "
        "would make a healthy session misread as expired if used as the probe object"
    )


# ---------------------------------------------------------------------------
# historic regression secondary: _PROBE_API_VERSION must match _API_VERSION in client.py.
# A comment alone won't prevent drift when client.py bumps its version.
# ---------------------------------------------------------------------------


def test_probe_api_version_matches_client_api_version() -> None:
    """_PROBE_API_VERSION in session_check must stay in sync with _API_VERSION in client.

    Both are intentionally separate constants (AGENTS.md forbids importing private names
    across modules), but they MUST agree or the session-check probe hits a different API
    version than the real SFDirectClient calls, which could produce false ACTIVE verdicts
    on version-specific endpoints.
    """
    from fieldkit.commands.sf.session_check import _PROBE_API_VERSION
    from fieldkit.sf.client import _API_VERSION

    assert _PROBE_API_VERSION == _API_VERSION, (
        f"_PROBE_API_VERSION ({_PROBE_API_VERSION!r}) in session_check.py must match "
        f"_API_VERSION ({_API_VERSION!r}) in sf/client.py. "
        "When bumping client._API_VERSION, also update session_check._PROBE_API_VERSION."
    )


# ---------------------------------------------------------------------------
# Additional coverage for CRAP gate reduction (spec 025)
# ---------------------------------------------------------------------------


def test_cookie_file_read_error_returns_false(tmp_path: Path, monkeypatch) -> None:
    """Exception reading cookie file → returns (False, error_msg)."""
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text("not-valid-json}{", encoding="utf-8")
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)
    alive, msg = check_sf_session()
    assert alive is False
    assert "Failed to read cookie file" in msg


def test_no_sid_cookie_returns_false(tmp_path: Path, monkeypatch) -> None:
    """Cookie file with no matching sid cookie → returns (False, error_msg)."""
    import json as _json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        _json.dumps({"cookies": [{"name": "other", "domain": "example.com", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)
    alive, msg = check_sf_session()
    assert alive is False
    assert "No 'sid' cookie" in msg


def test_sid_cookie_empty_value_returns_false(tmp_path: Path, monkeypatch) -> None:
    """sid cookie with empty value → returns (False, error_msg)."""
    import json as _json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        _json.dumps({"cookies": [{"name": "sid", "domain": ".examplecrm.my.salesforce.com", "value": ""}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)
    alive, msg = check_sf_session()
    assert alive is False
    assert "no value" in msg.lower() or "auth sf" in msg


def test_redirect_response_returns_false(tmp_path: Path, monkeypatch) -> None:
    """HTTP 302 redirect → session expired → (False, redirect msg)."""
    import json as _json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        _json.dumps({"cookies": [{"name": "sid", "domain": ".examplecrm.my.salesforce.com", "value": "abc123"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)

    class _FakeResp:
        status_code = 302

    monkeypatch.setattr("fieldkit.commands.sf.session_check.httpx.get", lambda *a, **kw: _FakeResp())
    alive, msg = check_sf_session()
    assert alive is False
    assert "redirect" in msg.lower() or "expired" in msg.lower()


def test_unexpected_status_code_returns_false(tmp_path: Path, monkeypatch) -> None:
    """Unexpected HTTP 500 → (False, error msg)."""
    import json as _json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        _json.dumps({"cookies": [{"name": "sid", "domain": ".examplecrm.my.salesforce.com", "value": "abc123"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)

    class _FakeResp:
        status_code = 500

    monkeypatch.setattr("fieldkit.commands.sf.session_check.httpx.get", lambda *a, **kw: _FakeResp())
    alive, msg = check_sf_session()
    assert alive is False
    assert "500" in msg or "Unexpected" in msg


def test_network_error_retry_exhausted_returns_false(tmp_path: Path, monkeypatch) -> None:
    """Network error on both attempts → (False, network error msg)."""
    import json as _json

    cookie_file = tmp_path / "sf-cookies.json"
    cookie_file.write_text(
        _json.dumps({"cookies": [{"name": "sid", "domain": ".examplecrm.my.salesforce.com", "value": "abc123"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("fieldkit.commands.sf.session_check.get_cookie_file", lambda: cookie_file)
    monkeypatch.setattr("fieldkit.commands.sf.session_check.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "fieldkit.commands.sf.session_check.httpx.get",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.RequestError("timeout")),
    )
    alive, msg = check_sf_session()
    assert alive is False
    assert "Network error" in msg
