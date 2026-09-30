"""Tests for fieldkit.commands.sf.session_check."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.session_check import check_sf_session, cli

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("status", "block", "exit_code"), [(200, 0, 0), (401, 1, 2), (302, 2, 2)])
def test_documentation_session_transcripts(tmp_path: Path, status: int, block: int, exit_code: int) -> None:
    document = Path("docs/guides/salesforce-auth.md").read_text(encoding="utf-8")
    transcripts = re.findall(r"```\n(Session:.*?)```", document, flags=re.DOTALL)
    assert len(transcripts) == 3
    cookie_file = _mock_cookie_file(
        tmp_path, {"cookies": [{"name": "sid", "value": "fictional", "domain": ".yourorg.my.salesforce.com"}]}
    )
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.httpx.get", return_value=httpx.Response(status)) as request,
    ):
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == exit_code, result.output
    assert result.output == transcripts[block]
    request.assert_called_once()
    assert "fictional" not in result.output


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


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("outcome", ["success", "unexpected", "network", "missing", "malformed"])
def test_session_diagnostics_are_payload_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, as_json: bool, outcome: str
) -> None:
    cookie_file = _mock_cookie_file(
        tmp_path,
        {
            "cookies": [
                {"name": "sid", "value": "private-token-sentinel", "domain": ".private-host-sentinel.my.salesforce.com"}
            ]
        },
    )
    if outcome == "missing":
        cookie_file.unlink()
    elif outcome == "malformed":
        cookie_file.write_text("private-malformed-sentinel", encoding="utf-8")
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.time.sleep"),
        patch(
            "fieldkit.commands.sf.session_check.httpx.get",
            return_value=httpx.Response(200 if outcome == "success" else 500),
            side_effect=httpx.ConnectError("private-network-sentinel") if outcome == "network" else None,
        ),
        caplog.at_level("DEBUG", logger="fieldkit.commands.sf.session_check"),
    ):
        result = CliRunner().invoke(cli, ["--json"] if as_json else [])
    assert result.exit_code == (0 if outcome == "success" else 2)
    assert "private-" not in result.output + caplog.text
    assert str(tmp_path) not in result.output + caplog.text


@pytest.mark.parametrize(
    "cookie_data",
    [
        {"cookies": [{"name": "sid", "value": "secret", "domain": "my.salesforce.com.example.com"}]},
        {"cookies": [{"name": "sid", "value": "secret", "domain": "evilmy.salesforce.com"}]},
        {"cookies": [{"name": "sid", "value": "secret", "domain": "org.my.salesforce.com@evil.example.com"}]},
        {"cookies": [{"name": "sid", "value": "secret", "domain": "org.my.salesforce.com/path"}]},
        {"cookies": [{"name": "sid", "value": {"secret": "value"}, "domain": "org.my.salesforce.com"}]},
        {"cookies": [None]},
        {"cookies": "private-malformed-sentinel"},
        [],
    ],
)
def test_unsafe_cookie_input_never_sends_credentials(tmp_path: Path, cookie_data: object) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(cookie_data), encoding="utf-8")
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.httpx.get", return_value=httpx.Response(200)) as request,
    ):
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 2
    request.assert_not_called()
    assert "private-" not in result.output


@pytest.mark.parametrize("lookalike_first", [True, False])
def test_mixed_valid_and_lookalike_sid_selects_only_valid_cookie(tmp_path: Path, lookalike_first: bool) -> None:
    valid = {"name": "sid", "value": "selected-session-sentinel", "domain": ".org.my.salesforce.com"}
    lookalike = {"name": "sid", "value": "ignored-session-sentinel", "domain": "org.my.salesforce.com.evil.example.com"}
    cookies = [lookalike, valid] if lookalike_first else [valid, lookalike]
    cookie_file = _mock_cookie_file(tmp_path, {"cookies": cookies})
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.httpx.get", return_value=httpx.Response(200)) as request,
    ):
        result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    request.assert_called_once()
    assert request.call_args.args[0].startswith("https://org.my.salesforce.com/")
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer selected-session-sentinel"}
    assert "selected-session-sentinel" not in result.output
    assert "ignored-session-sentinel" not in result.output
    assert "evil.example.com" not in result.output


def test_session_active(tmp_path: Path) -> None:
    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("httpx.get", return_value=_make_response(200)),
    ):
        alive, msg = check_sf_session()
    assert alive is True
    assert msg == "Salesforce API session is active."


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
# The session probe uses Bearer authentication rather than browser-cookie headers.
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
# Probe Account metadata: the API root does not establish object-level access.
# ---------------------------------------------------------------------------


def test_probe_hits_account_describe_endpoint(tmp_path: Path) -> None:
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


def test_probe_uses_canonical_api_version(tmp_path: Path) -> None:
    """The health probe targets the same REST version as domain requests."""
    from fieldkit.sf.client import API_VERSION

    cookie_file = _mock_cookie_file(tmp_path)
    with (
        patch("fieldkit.commands.sf.session_check.get_cookie_file", return_value=cookie_file),
        patch("fieldkit.commands.sf.session_check.httpx.get", return_value=httpx.Response(200)) as request,
    ):
        result = check_sf_session()

    assert result[0] is True
    assert request.call_args.args[0] == (
        f"https://examplecrm.my.salesforce.com/services/data/{API_VERSION}/sobjects/Account/describe"
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
