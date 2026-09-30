"""Credential selection rejects redirects, ambiguity, and malformed local data."""

import json
import os
from pathlib import Path

import pytest

from fieldkit.config.salesforce_cookie import MAX_COOKIE_FILE_BYTES, read_salesforce_cookie

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("domain", ["my.salesforce.com", ".acme.my.salesforce.com", "ACME.MY.SALESFORCE.COM"])
def test_selects_exact_salesforce_domain(tmp_path: Path, domain: str) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps({"cookies": [{"name": "sid", "value": "private-token-sentinel", "domain": domain}]}),
        encoding="utf-8",
    )
    result = read_salesforce_cookie(path)
    assert result is not None
    assert result.sid == "private-token-sentinel"
    assert result.domain == domain.removeprefix(".").lower()
    assert "private-token-sentinel" not in repr(result)


@pytest.mark.parametrize("kind", ["duplicate", "ambiguous", "oversized", "fifo", "symlink", "utf8", "header"])
def test_refuses_unsafe_cookie_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "cookies.json"
    cookie = {"name": "sid", "value": "secret", "domain": "acme.my.salesforce.com"}
    if kind == "duplicate":
        path.write_text('{"cookies": [], "cookies": []}', encoding="utf-8")
    elif kind == "ambiguous":
        path.write_text(json.dumps({"cookies": [cookie, cookie]}), encoding="utf-8")
    elif kind == "oversized":
        path.write_bytes(b" " * (MAX_COOKIE_FILE_BYTES + 1))
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text('{"cookies": []}', encoding="utf-8")
        path.symlink_to(target)
    elif kind == "utf8":
        path.write_bytes(b"\xff")
    else:
        cookie["value"] = "secret\r\nInjected: value"
        path.write_text(json.dumps({"cookies": [cookie]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read a valid Salesforce session cookie"):
        read_salesforce_cookie(path)
