"""Verify receipt bytes against externally selected SSH signer expectations.

This preparatory helper does not authenticate the caller's trust selection or
grant release authority. Callers must retain their release authorization gates.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scripts import release_filesystem

SSH_KEYGEN = "/usr/bin/ssh-keygen"
SIGNATURE_VERIFY_TIMEOUT_SECONDS = 10
MAX_RECEIPT_BYTES = 5 * 1024 * 1024
MAX_SIGNATURE_CONTROL_BYTES = 64 * 1024
MAX_VERIFY_OUTPUT_BYTES = 4096


@dataclass(frozen=True, kw_only=True)
class VerifiedSignature:
    """Observed byte digest and signer binding, without release authorization."""

    receipt_sha256: str
    principal: str
    namespace: str
    key_fingerprint: str


def _read_input(path: Path, *, maximum_bytes: int) -> bytes:
    """Snapshot bounded regular files through real directory components."""
    try:
        parent_fd = release_filesystem.open_real_directory(path.parent)
        try:
            descriptor = os.open(path.name, release_filesystem.file_flags(), dir_fd=parent_fd)
            try:
                data = release_filesystem.read_regular_file(descriptor, maximum_bytes=maximum_bytes)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    except (OSError, ValueError):
        raise ValueError("signature input is unavailable, non-regular, or exceeds its byte limit") from None
    if not data:
        raise ValueError("signature input is empty")
    return data


def verify(
    receipt: Path,
    *,
    allowed_signers: Path,
    principal: str,
    namespace: str,
    signature: Path,
    expected_key_fingerprint: str,
    revocation_file: Path,
) -> VerifiedSignature:
    """Verify exact receipt bytes using OpenSSH and caller-selected trust files.

    All files are snapshotted before invoking the fixed system executable. A
    selected revocation list (OpenSSH KRL or public-key list) is mandatory;
    unavailable or malformed lists never trigger a fallback.
    The expected fingerprint binds the actual signing key, not a certificate
    authority. Unexpected OpenSSH success-output formats fail closed.
    """
    for value in (principal, namespace):
        if re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", value) is None:
            raise ValueError("signature principal or namespace is invalid")
    if re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected_key_fingerprint) is None:
        raise ValueError("signature expected key fingerprint is invalid")

    receipt_bytes = _read_input(receipt, maximum_bytes=MAX_RECEIPT_BYTES)
    inputs = {
        "allowed-signers": _read_input(allowed_signers, maximum_bytes=MAX_SIGNATURE_CONTROL_BYTES),
        "signature": _read_input(signature, maximum_bytes=MAX_SIGNATURE_CONTROL_BYTES),
        "revocations": _read_input(revocation_file, maximum_bytes=MAX_SIGNATURE_CONTROL_BYTES),
    }

    try:
        with tempfile.TemporaryDirectory(prefix="fieldkit-signature-") as scratch:
            root = Path(scratch)
            for name, data in inputs.items():
                (root / name).write_bytes(data)
            argv = [
                SSH_KEYGEN,
                "-Y",
                "verify",
                "-f",
                str(root / "allowed-signers"),
                "-I",
                principal,
                "-n",
                namespace,
                "-s",
                str(root / "signature"),
                "-r",
                str(root / "revocations"),
            ]
            # Keep tool diagnostics private; accept only bounded verification output.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    argv,
                    input=receipt_bytes,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=SIGNATURE_VERIFY_TIMEOUT_SECONDS,
                    env={"LC_ALL": "C"},
                )
                output.seek(0)
                verified_output = output.read(MAX_VERIFY_OUTPUT_BYTES + 1)
    except subprocess.TimeoutExpired:
        raise ValueError("signature verification timed out") from None
    except OSError:
        raise ValueError("signature verification tool or scratch storage is unavailable") from None

    if result.returncode != 0:
        raise ValueError("signature verification failed")
    expected_output = (
        rf'Good "{re.escape(namespace)}" signature for {re.escape(principal)} with '
        rf"[A-Za-z0-9_-]+ key {re.escape(expected_key_fingerprint)}\n"
    )
    if (
        len(verified_output) > MAX_VERIFY_OUTPUT_BYTES
        or re.fullmatch(expected_output.encode("ascii"), verified_output) is None
    ):
        raise ValueError("signature verification signer binding or output format is invalid")
    return VerifiedSignature(
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        principal=principal,
        namespace=namespace,
        key_fingerprint=expected_key_fingerprint,
    )
