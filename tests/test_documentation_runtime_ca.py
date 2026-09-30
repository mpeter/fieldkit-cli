"""Exact public CA closure and actual offline ensurepip runtime coverage."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from scripts import documentation_ca as ca
from scripts import documentation_command_runner as runner

pytestmark = pytest.mark.unit
_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_KEY_BEGIN = b"-----BEGIN " + b"PRIVATE KEY" + b"-----"
_PRIVATE_KEY_END = b"-----END " + b"PRIVATE KEY" + b"-----"


@pytest.fixture
def public_bundle() -> bytes:
    source = next(path for path in ca._SYSTEM_CA_BUNDLE_PATHS if path.exists())
    data = ca._read_system_ca_bundle(source)
    assert data.startswith(b"#") or data.startswith(b"-----BEGIN")
    return data


def test_exact_ca_endpoints_are_fixed_and_have_no_environment_selection() -> None:
    assert {str(path) for path in ca._SYSTEM_CA_BUNDLE_PATHS} == {
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/pki/tls/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        "/etc/ssl/ca-bundle.pem",
    }


def test_alias_snapshots_are_exact_anonymous_read_only_and_survive_replacement(
    tmp_path: Path, public_bundle: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bundle.pem"
    source.write_bytes(public_bundle)
    alias = tmp_path / "cert.pem"
    alias.symlink_to(source)
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (source, alias))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "must-not-read-secret"))
    descriptors: list[int] = []
    try:
        options = ca.system_ca_mounts(tmp_path, descriptors)
        assert options.count("--ro-bind-data") == 2
        assert options[-1] == str(alias)
        source.write_bytes(b"changed")
        for descriptor in descriptors:
            assert os.read(descriptor, ca._MAX_CA_BUNDLE_BYTES + 1) == public_bundle
            metadata = os.fstat(descriptor)
            assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 0
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.write(descriptor, b"forbidden")
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.parametrize(
    "kind", ["empty", "oversized", "fifo", "directory", "key", "garbage", "whitespace", "unclosed"]
)
def test_unsafe_bundle_inputs_fail_before_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    source = tmp_path / "bundle.pem"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    elif kind == "oversized":
        with source.open("wb") as stream:
            stream.truncate(ca._MAX_CA_BUNDLE_BYTES + 1)
    else:
        data = {
            "empty": b"",
            "key": _PRIVATE_KEY_BEGIN + b"\nQQ==\n" + _PRIVATE_KEY_END + b"\n",
            "garbage": b"password=secret\n",
            "whitespace": b" " * 100_000 + b"invalid",
            "unclosed": b"-----BEGIN CERTIFICATE-----\nQQ==\n",
        }[kind]
        source.write_bytes(data)
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (source,))
    with pytest.raises(ValueError, match="system CA bundle"):
        ca._read_system_ca_bundle(source)


def test_alias_cannot_expose_any_other_host_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "credentials"
    secret.write_text("must-not-read", encoding="utf-8")
    alias = tmp_path / "cert.pem"
    alias.symlink_to(secret)
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (alias,))
    with pytest.raises(ValueError, match="alias escapes"):
        ca._read_system_ca_bundle(alias)


def test_missing_ca_closure_fails_without_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (tmp_path / "missing.pem",))
    with pytest.raises(OSError, match="requires a supported public system CA bundle"):
        ca.system_ca_mounts(tmp_path, [])


def test_invalid_certificate_encoding_is_not_a_mountable_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bundle.pem"
    source.write_bytes(b"-----BEGIN CERTIFICATE-----\nQQ==\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (source,))
    descriptors: list[int] = []
    try:
        with pytest.raises(ValueError, match="certificate parsing failed"):
            ca.system_ca_mounts(tmp_path, descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.integration
def test_real_contained_venv_ensurepip_has_only_read_only_public_ca_inputs(tmp_path: Path) -> None:
    paths = tuple(str(path) for path in ca._SYSTEM_CA_BUNDLE_PATHS if path.exists())
    program = f"""
import os, pathlib, subprocess, venv
for name in {paths!r}:
    path = pathlib.Path(name)
    assert path.read_bytes()
    assert os.statvfs(path).f_flag & os.ST_RDONLY
    try:
        path.write_bytes(b'forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('CA file was writable')
for name in ('/etc/passwd', '/etc/shadow', '/etc/ssh', '/etc/pki/tls/private'):
    assert not pathlib.Path(name).exists(), name
VENV_PIP_TIMEOUT_SECONDS = 30
root = pathlib.Path(os.environ['TMPDIR']) / 'offline-venv'
venv.EnvBuilder(with_pip=True).create(root)
result = subprocess.run([str(root / 'bin/python'), '-m', 'pip', '--version'], capture_output=True, text=True, check=True, timeout=VENV_PIP_TIMEOUT_SECONDS)
assert 'pip ' in result.stdout
print('offline-ensurepip-created')
"""
    result = runner._run_bounded(
        tmp_path, (sys.executable, "-I", "-c", program), runtime_root=_RUNTIME_ROOT, timeout=60
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "offline-ensurepip-created"


def test_outside_comments_never_enter_anonymous_certificate_snapshot(
    tmp_path: Path, public_bundle: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bundle.pem"
    source.write_bytes(b"# credential=fictional-secret\n\n" + public_bundle + b"\n# trailing unrelated comment\n")
    monkeypatch.setattr(ca, "_SYSTEM_CA_BUNDLE_PATHS", (source,))
    descriptors: list[int] = []
    try:
        result = ca.system_ca_mounts(tmp_path, descriptors)
        assert result[-1] == str(source)
        snapshot = os.read(descriptors[0], ca._MAX_CA_BUNDLE_BYTES + 1)
        assert snapshot == public_bundle
        assert b"fictional-secret" not in snapshot
        assert b"#" not in snapshot
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_normalization_preserves_certificate_labels_payload_and_order() -> None:
    plain = b"-----BEGIN CERTIFICATE-----\r\nQQ==\r\n-----END CERTIFICATE-----\r\n"
    trusted = b"-----BEGIN TRUSTED CERTIFICATE-----\n\nQg==\n-----END TRUSTED CERTIFICATE-----\n"
    result = ca._normalize_ca_bundle_content(b"# description\n\n" + plain + b"\n# separator\n" + trusted)
    assert result == plain + trusted


def test_private_key_marker_is_rejected_even_in_an_omitted_comment(public_bundle: bytes) -> None:
    with pytest.raises(ValueError, match="only certificates"):
        ca._normalize_ca_bundle_content(public_bundle + b"# " + _PRIVATE_KEY_BEGIN + b"\n")
