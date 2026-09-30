"""Preparatory signature verification using disposable local fixture keys."""

import hashlib
import io
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import release_signature

pytestmark = pytest.mark.integration
KEY_OPERATION_TIMEOUT_SECONDS = 10
PRINCIPAL = "approver@example.com"
NAMESPACE = "fieldkit-release-receipt"


def _keygen(*arguments: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        [release_signature.SSH_KEYGEN, *arguments],
        input=input_bytes,
        capture_output=True,
        check=True,
        timeout=KEY_OPERATION_TIMEOUT_SECONDS,
        env={"LC_ALL": "C"},
    ).stdout


@dataclass(frozen=True)
class SignatureFixture:
    receipt: Path
    allowed_signers: Path
    signature: Path
    revocation_file: Path
    expected_key_fingerprint: str
    key: Path
    other_key: Path
    principal: str = PRINCIPAL
    namespace: str = NAMESPACE


@pytest.fixture
def signed_receipt(tmp_path: Path) -> SignatureFixture:
    key = tmp_path / "fixture-key"
    other_key = tmp_path / "other-fixture-key"
    _keygen("-q", "-t", "ed25519", "-N", "", "-C", "fixture", "-f", str(key))
    _keygen("-q", "-t", "ed25519", "-N", "", "-C", "fixture", "-f", str(other_key))
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b'{"schema_version":1,"decision":"fixture"}\r\n')
    _keygen("-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(receipt))
    allowed_signers = tmp_path / "allowed-signers"
    allowed_signers.write_text(
        f'{PRINCIPAL} namespaces="{NAMESPACE}" {key.with_suffix(".pub").read_text(encoding="utf-8")}',
        encoding="utf-8",
    )
    revocation_file = tmp_path / "revocations"
    revocation_file.write_bytes(other_key.with_suffix(".pub").read_bytes())
    fingerprint = _keygen("-l", "-E", "sha256", "-f", str(key.with_suffix(".pub"))).decode().split()[1]
    return SignatureFixture(
        receipt, allowed_signers, Path(str(receipt) + ".sig"), revocation_file, fingerprint, key, other_key
    )


def _verify(fixture: SignatureFixture) -> release_signature.VerifiedSignature:
    return release_signature.verify(
        fixture.receipt,
        allowed_signers=fixture.allowed_signers,
        principal=fixture.principal,
        namespace=fixture.namespace,
        signature=fixture.signature,
        expected_key_fingerprint=fixture.expected_key_fingerprint,
        revocation_file=fixture.revocation_file,
    )


def test_valid_signature_binds_exact_bytes(signed_receipt: SignatureFixture) -> None:
    result = _verify(signed_receipt)
    assert result == release_signature.VerifiedSignature(
        receipt_sha256=hashlib.sha256(signed_receipt.receipt.read_bytes()).hexdigest(),
        principal=PRINCIPAL,
        namespace=NAMESPACE,
        key_fingerprint=signed_receipt.expected_key_fingerprint,
    )


@pytest.mark.parametrize("field,value", [("principal", "other@example.com"), ("namespace", "other-namespace")])
def test_wrong_signer_expectations_fail(signed_receipt: SignatureFixture, field: str, value: str) -> None:
    fixture = (
        replace(signed_receipt, principal=value) if field == "principal" else replace(signed_receipt, namespace=value)
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        _verify(fixture)


def test_altered_receipt_bytes_fail(signed_receipt: SignatureFixture) -> None:
    signed_receipt.receipt.write_bytes(signed_receipt.receipt.read_bytes().replace(b"\r\n", b"\n"))
    with pytest.raises(ValueError, match="signature verification failed"):
        _verify(signed_receipt)


def test_valid_other_key_does_not_satisfy_expected_fingerprint(signed_receipt: SignatureFixture) -> None:
    other_public_key = signed_receipt.other_key.with_suffix(".pub").read_text(encoding="utf-8")
    signed_receipt.allowed_signers.write_text(f"{PRINCIPAL} {other_public_key}", encoding="utf-8")
    signed_receipt.revocation_file.write_bytes(signed_receipt.key.with_suffix(".pub").read_bytes())
    signed_receipt.signature.unlink()
    _keygen("-Y", "sign", "-f", str(signed_receipt.other_key), "-n", NAMESPACE, str(signed_receipt.receipt))
    with pytest.raises(ValueError, match="signer binding"):
        _verify(signed_receipt)


@pytest.mark.parametrize("format_name", ["public-key-list", "krl"])
def test_revoked_key_fails(signed_receipt: SignatureFixture, format_name: str) -> None:
    if format_name == "public-key-list":
        signed_receipt.revocation_file.write_bytes(signed_receipt.key.with_suffix(".pub").read_bytes())
    else:
        signed_receipt.revocation_file.unlink()
        _keygen("-k", "-f", str(signed_receipt.revocation_file), str(signed_receipt.key.with_suffix(".pub")))
    with pytest.raises(ValueError, match="signature verification failed"):
        _verify(signed_receipt)


def test_valid_signature_with_krl(signed_receipt: SignatureFixture) -> None:
    signed_receipt.revocation_file.unlink()
    _keygen("-k", "-f", str(signed_receipt.revocation_file), str(signed_receipt.other_key.with_suffix(".pub")))
    assert _verify(signed_receipt).key_fingerprint == signed_receipt.expected_key_fingerprint


@pytest.mark.parametrize("field", ["receipt", "allowed_signers", "signature", "revocation_file"])
@pytest.mark.parametrize("problem", ["missing", "empty", "malformed", "oversized", "directory", "symlink", "fifo"])
def test_invalid_files_fail(signed_receipt: SignatureFixture, field: str, problem: str) -> None:
    path = getattr(signed_receipt, field)
    assert isinstance(path, Path)
    original = path.read_bytes()
    path.unlink()
    if problem == "empty":
        path.write_bytes(b"")
    elif problem == "malformed":
        path.write_bytes(b"invalid signature input\n")
    elif problem == "oversized":
        limit = (
            release_signature.MAX_RECEIPT_BYTES if field == "receipt" else release_signature.MAX_SIGNATURE_CONTROL_BYTES
        )
        path.write_bytes(b"x" * (limit + 1))
    elif problem == "directory":
        path.mkdir()
    elif problem == "symlink":
        target = path.with_name(path.name + "-target")
        target.write_bytes(original)
        path.symlink_to(target)
    elif problem == "fifo":
        os.mkfifo(path)
    with pytest.raises(ValueError, match="signature"):
        _verify(signed_receipt)


def test_symlink_directory_is_rejected(signed_receipt: SignatureFixture, tmp_path: Path) -> None:
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="signature input"):
        _verify(replace(signed_receipt, receipt=linked_directory / signed_receipt.receipt.name))


@pytest.mark.parametrize("value", ["", "bad\nvalue", "bad\x00value", "a" * 257])
@pytest.mark.parametrize("field", ["principal", "namespace", "expected_key_fingerprint"])
def test_invalid_selection_fails(signed_receipt: SignatureFixture, field: str, value: str) -> None:
    if field == "principal":
        fixture = replace(signed_receipt, principal=value)
    elif field == "namespace":
        fixture = replace(signed_receipt, namespace=value)
    else:
        fixture = replace(signed_receipt, expected_key_fingerprint=value)
    with pytest.raises(ValueError, match="invalid"):
        _verify(fixture)


def test_timeout_fails_closed(signed_receipt: SignatureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Mock(side_effect=subprocess.TimeoutExpired("fixture", 10, stderr=b"private fixture text"))
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(ValueError, match=r"^signature verification timed out$"):
        _verify(signed_receipt)
    assert runner.call_args.kwargs["timeout"] == release_signature.SIGNATURE_VERIFY_TIMEOUT_SECONDS
    assert runner.call_args.kwargs["input"] == signed_receipt.receipt.read_bytes()
    assert runner.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert "shell" not in runner.call_args.kwargs


def test_unavailable_tool_fails(signed_receipt: SignatureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_signature, "SSH_KEYGEN", "/missing-fixture-tool")
    with pytest.raises(ValueError, match="tool or scratch storage is unavailable"):
        _verify(signed_receipt)


def test_success_without_expected_output_fails(
    signed_receipt: SignatureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Mock(return_value=subprocess.CompletedProcess(["fixture"], 0))
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(ValueError, match="signer binding or output format"):
        _verify(signed_receipt)


@pytest.mark.parametrize("output_bytes", [b"x" * (release_signature.MAX_VERIFY_OUTPUT_BYTES + 1), b"format drift\n"])
def test_unexpected_tool_output_fails(
    signed_receipt: SignatureFixture, monkeypatch: pytest.MonkeyPatch, output_bytes: bytes
) -> None:
    def fake_run(_arguments: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = kwargs["stdout"]
        assert isinstance(output, io.BufferedIOBase)
        output.write(output_bytes)
        return subprocess.CompletedProcess(["fixture"], 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="signer binding or output format"):
        _verify(signed_receipt)
