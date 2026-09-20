"""Read release archives through deterministic path and resource bounds."""

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ArchiveLimits:
    """Resource ceilings applied before archive members become scan inputs."""

    max_members: int
    max_member_bytes: int
    max_total_bytes: int
    max_archive_bytes: int


@dataclass(frozen=True)
class ArtifactDocument:
    """One archive member and its corresponding repository policy path."""

    subject_path: str
    policy_path: str
    data: bytes


@dataclass(frozen=True)
class ArtifactEvidence:
    """Payload-safe identity and coverage evidence for one archive."""

    name: str
    kind: str
    sha256: str
    member_count: int
    total_uncompressed_bytes: int


def _canonical_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\0" in name:
        raise ValueError(f"noncanonical archive member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name != path.as_posix():
        raise ValueError(f"noncanonical archive member: {name!r}")
    return path


def _check_inventory(names: list[str], sizes: list[int], limits: ArchiveLimits) -> None:
    if len(names) > limits.max_members:
        raise ValueError("archive member count exceeds limit")
    if len(names) != len(set(names)):
        raise ValueError("duplicate archive member")
    if any(size < 0 or size > limits.max_member_bytes for size in sizes):
        raise ValueError("archive member exceeds size limit")
    if sum(sizes) > limits.max_total_bytes:
        raise ValueError("archive uncompressed size exceeds limit")


def _capture(path: Path, limits: ArchiveLimits) -> tuple[bytes, str]:
    with path.open("rb") as stream:
        data = stream.read(limits.max_archive_bytes + 1)
    if len(data) > limits.max_archive_bytes:
        raise ValueError("compressed archive exceeds size limit")
    return data, hashlib.sha256(data).hexdigest()


def _wheel_policy_path(name: str) -> str:
    if name.startswith("fieldkit/"):
        return f"src/{name}"
    if name.endswith(".dist-info/METADATA"):
        return "pyproject.toml"
    return name


def _sdist_policy_path(name: str) -> str:
    if name == "PKG-INFO":
        return "pyproject.toml"
    return name


def _read_wheel(
    path: Path,
    data: bytes,
    digest: str,
    limits: ArchiveLimits,
) -> tuple[ArtifactEvidence, tuple[ArtifactDocument, ...]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        inventory = archive.infolist()
        names = [info.filename for info in inventory]
        sizes = [info.file_size for info in inventory]
        _check_inventory(names, sizes, limits)
        documents: list[ArtifactDocument] = []
        for info in inventory:
            canonical = _canonical_member_path(info.filename.rstrip("/"))
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if info.is_dir():
                if file_type not in {0, stat.S_IFDIR}:
                    raise ValueError(f"unsupported archive member type: {info.filename}")
                continue
            if file_type not in {0, stat.S_IFREG}:
                raise ValueError(f"unsupported archive member type: {info.filename}")
            if info.flag_bits & 0x1:
                raise ValueError(f"encrypted archive member is unsupported: {info.filename}")
            with archive.open(info) as stream:
                data = stream.read(limits.max_member_bytes + 1)
            if len(data) != info.file_size or len(data) > limits.max_member_bytes:
                raise ValueError(f"archive member size mismatch: {info.filename}")
            name = canonical.as_posix()
            documents.append(
                ArtifactDocument(
                    subject_path=f"artifact:{path.name}:{name}",
                    policy_path=_wheel_policy_path(name),
                    data=data,
                )
            )
    return (
        ArtifactEvidence(path.name, "wheel", digest, len(documents), sum(len(doc.data) for doc in documents)),
        tuple(sorted(documents, key=lambda document: document.subject_path)),
    )


def _read_sdist(
    path: Path,
    data: bytes,
    digest: str,
    limits: ArchiveLimits,
) -> tuple[ArtifactEvidence, tuple[ArtifactDocument, ...]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
        names: set[str] = set()
        root: str | None = None
        total_size = 0
        documents: list[ArtifactDocument] = []
        for member in archive:
            if len(names) >= limits.max_members:
                raise ValueError("archive member count exceeds limit")
            if member.name in names:
                raise ValueError("duplicate archive member")
            names.add(member.name)
            canonical = _canonical_member_path(member.name.rstrip("/"))
            member_root = canonical.parts[0]
            if root is None:
                root = member_root
            elif member_root != root:
                raise ValueError("source distribution must contain exactly one root")
            if member.size < 0 or member.size > limits.max_member_bytes:
                raise ValueError("archive member exceeds size limit")
            total_size += member.size
            if total_size > limits.max_total_bytes:
                raise ValueError("archive uncompressed size exceeds limit")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"unsupported archive member type: {member.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"archive member is unreadable: {member.name}")
            with stream:
                data = stream.read(limits.max_member_bytes + 1)
            if len(data) != member.size or len(data) > limits.max_member_bytes:
                raise ValueError(f"archive member size mismatch: {member.name}")
            if root is None:
                raise ValueError("source distribution must contain exactly one root")
            relative = canonical.as_posix().removeprefix(f"{root}/")
            documents.append(
                ArtifactDocument(
                    subject_path=f"artifact:{path.name}:{canonical.as_posix()}",
                    policy_path=_sdist_policy_path(relative),
                    data=data,
                )
            )
    return (
        ArtifactEvidence(path.name, "sdist", digest, len(documents), sum(len(doc.data) for doc in documents)),
        tuple(sorted(documents, key=lambda document: document.subject_path)),
    )


def read_artifact(path: Path, limits: ArchiveLimits) -> tuple[ArtifactEvidence, tuple[ArtifactDocument, ...]]:
    """Return bounded regular members from one supported release archive."""
    data, digest = _capture(path, limits)
    if path.suffix == ".whl":
        return _read_wheel(path, data, digest, limits)
    if path.name.endswith(".tar.gz"):
        return _read_sdist(path, data, digest, limits)
    raise ValueError(f"unsupported artifact type: {path.name}")
