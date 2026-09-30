"""Certificate-only inputs shared by documentation preparation and execution."""

import os
import ssl
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.documentation_runtime import directory_options
else:  # pragma: no cover - direct script execution
    from documentation_runtime import directory_options

# Only public certificate bundles at fixed distro endpoints supplement /etc.
_SYSTEM_CA_BUNDLE_PATHS = tuple(
    Path(path)
    for path in (
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/pki/tls/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        "/etc/ssl/ca-bundle.pem",
    )
)
_MAX_CA_BUNDLE_BYTES = 2 * 1024 * 1024
_CA_BASE64_BYTES = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def _normalize_ca_bundle_content(data: bytes) -> bytes:
    """Validate linearly and emit only ordered PEM blocks, preserving block bytes.

    Permitted comments and blank lines outside certificate blocks are explicitly
    omitted. Nothing inside a certificate/TRUSTED CERTIFICATE block is filtered;
    private keys anywhere and arbitrary noncomment text fail before emission.
    """
    if b"PRIVATE KEY" in data:
        raise ValueError("system CA bundle must contain only certificates and generated comments")
    label: bytes | None = None
    certificate_count = 0
    has_payload = False
    blocks: list[bytes] = []
    for raw_line in data.splitlines(keepends=True):
        line = raw_line.strip()
        if label is not None:
            blocks.append(raw_line)
        if not line:
            continue
        if label is None:
            if line.startswith(b"#"):
                continue
            if line == b"-----BEGIN CERTIFICATE-----":
                label = b"CERTIFICATE"
            elif line == b"-----BEGIN TRUSTED CERTIFICATE-----":
                label = b"TRUSTED CERTIFICATE"
            else:
                raise ValueError("system CA bundle must contain only certificates and generated comments")
            has_payload = False
            blocks.append(raw_line)
        elif line == b"-----END " + label + b"-----":
            if not has_payload:
                raise ValueError("system CA bundle certificate payload is empty")
            certificate_count += 1
            label = None
        elif line.translate(None, _CA_BASE64_BYTES):
            raise ValueError("system CA bundle certificate payload is invalid")
        else:
            has_payload = True
    if label is not None or certificate_count == 0:
        raise ValueError("system CA bundle certificate blocks are incomplete or absent")
    return b"".join(blocks)


def _read_system_ca_bundle(source: Path) -> bytes:
    """Capture a bounded fixed CA file and normalize to certificate-only PEM bytes."""
    if TYPE_CHECKING or __package__:
        from scripts import release_filesystem
    else:  # pragma: no cover - direct script execution
        import release_filesystem

    if source not in _SYSTEM_CA_BUNDLE_PATHS:
        raise ValueError("system CA bundle source is outside the fixed certificate closure")
    resolved = source.resolve(strict=True)
    if resolved not in _SYSTEM_CA_BUNDLE_PATHS:
        raise ValueError("system CA bundle alias escapes the fixed certificate closure")
    parent = release_filesystem.open_real_directory(resolved.parent)
    try:
        descriptor = os.open(resolved.name, release_filesystem.file_flags() | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_CA_BUNDLE_BYTES:
            raise ValueError("system CA bundle must be a nonempty bounded regular file")
        data = release_filesystem.read_regular_file(descriptor, maximum_bytes=_MAX_CA_BUNDLE_BYTES)
        after = os.fstat(descriptor)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        if identity(before) != identity(after) or len(data) != before.st_size:
            raise ValueError("system CA bundle changed during capture or exceeds limit")
        return _normalize_ca_bundle_content(data)
    finally:
        os.close(descriptor)


def system_ca_mounts(temporary_root: Path, mount_descriptors: list[int]) -> list[str]:
    """Snapshot normalized certificate-only PEM bytes as anonymous read-only mounts.

    The supplemental documentation runtime is preparatory, not independently
    approved C0. Caller environment and custom CA selectors are not consulted.
    """
    options: list[str] = []
    for source in _SYSTEM_CA_BUNDLE_PATHS:
        if not source.exists() and not source.is_symlink():
            continue
        data = _read_system_ca_bundle(source)
        with tempfile.TemporaryFile(dir=temporary_root) as stream:
            if stream.write(data) != len(data):
                raise OSError("system CA snapshot write was incomplete")
            stream.flush()
            descriptor = os.open(f"/proc/self/fd/{stream.fileno()}", os.O_RDONLY | os.O_CLOEXEC)
            mount_descriptors.append(descriptor)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                context.load_verify_locations(cafile=f"/proc/self/fd/{descriptor}")
            except ssl.SSLError as error:
                raise ValueError("system CA bundle certificate parsing failed") from error
            if not context.cert_store_stats()["x509"]:
                raise ValueError("system CA bundle contains no usable certificates")
        options.extend(directory_options(source.parent))
        options.extend(("--ro-bind-data", str(descriptor), str(source)))
    if not options:
        raise OSError("documentation runtime requires a supported public system CA bundle")
    return options
