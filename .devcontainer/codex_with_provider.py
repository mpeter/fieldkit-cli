#!/usr/local/bin/python
"""Adapt an opaque subscription bearer to the native Codex auth file."""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import time
from pathlib import Path


def encode(value: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def main() -> None:
    access_token = os.environ.get("CODEX_AUTH_ACCESS_TOKEN", "")
    account_id = os.environ.get("CODEX_ACCOUNT_ID", "")
    if not access_token.startswith("openshell:resolve:") or not account_id:
        raise ValueError("an opaque OpenShell bearer and account routing metadata are required")
    identity = {
        "iss": "https://auth.openai.com",
        "aud": "codex",
        "sub": "openshell-worker",
        "email": "worker@example.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    id_token = f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(identity)}.placeholder"
    auth = {
        "auth_mode": "chatgptAuthTokens",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": access_token,
            "refresh_token": "",
            "id_token": id_token,
            "account_id": account_id,
        },
        "last_refresh": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
    }
    directory = Path.home() / ".codex"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(directory_descriptor, 0o700)
        descriptor = os.open(
            "auth.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=directory_descriptor
        )
    finally:
        os.close(directory_descriptor)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(auth, stream)
    os.execv("/usr/local/bin/codex", ["codex", *sys.argv[1:]])


if __name__ == "__main__":
    main()
