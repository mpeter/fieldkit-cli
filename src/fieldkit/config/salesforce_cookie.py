"""Bounded selection of one REST-capable Salesforce browser session."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from fieldkit.util.json_decode import unique_json_object
from fieldkit.util.text_snapshot import read_text_snapshot

MAX_COOKIE_FILE_BYTES = 1_000_000
MAX_COOKIES = 1000


@dataclass(frozen=True, repr=False)
class SalesforceCookie:
    """Validated credential and exact HTTPS destination; never render this object."""

    sid: str
    domain: str


def normalize_salesforce_cookie_domain(raw: str) -> str | None:
    """Return an exact REST-cookie hostname, never URL or authority syntax."""
    domain = raw.removeprefix(".").lower()
    if (
        len(domain) > 253
        or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*my\.salesforce\.com", domain) is None
    ):
        return None
    return domain


def validate_salesforce_sid(sid: object) -> str:
    """Require a bounded credential safe for a single Authorization header."""
    if not isinstance(sid, str) or not 1 <= len(sid) <= 8192 or any(not 33 <= ord(c) <= 126 for c in sid):
        raise ValueError("Invalid session credential")
    return sid


def read_salesforce_cookie(path: Path) -> SalesforceCookie | None:
    """Select one unambiguous session without following a leaf symlink.

    Missing files propagate FileNotFoundError. Invalid or ambiguous cookie data
    raises a fixed ValueError; no credentials or server names enter diagnostics.
    """
    try:
        text = read_text_snapshot(path, max_bytes=MAX_COOKIE_FILE_BYTES).content
        data = json.loads(text, object_pairs_hook=unique_json_object)
        if not isinstance(data, dict):
            raise ValueError("Invalid cookie document")
        cookies = data.get("cookies", [])
        if not isinstance(cookies, list) or len(cookies) > MAX_COOKIES:
            raise ValueError("Invalid cookie collection")
        selected: SalesforceCookie | None = None
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise ValueError("Invalid cookie entry")
            if cookie.get("name") != "sid":
                continue
            raw_domain = cookie.get("domain")
            if not isinstance(raw_domain, str):
                raise ValueError("Invalid cookie domain")
            domain = normalize_salesforce_cookie_domain(raw_domain)
            if domain is None:
                continue
            sid = validate_salesforce_sid(cookie.get("value"))
            if selected is not None:
                raise ValueError("Ambiguous session credentials")
            selected = SalesforceCookie(sid=sid, domain=domain)
        return selected
    except (ValueError, RecursionError):
        raise ValueError("Cannot read a valid Salesforce session cookie") from None
