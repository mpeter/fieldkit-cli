"""Contracts for bounded public release archive traversal."""

import hashlib
import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import public_tree_artifacts

pytestmark = pytest.mark.unit


def _limits(*, max_member_bytes: int = 128) -> public_tree_artifacts.ArchiveLimits:
    return public_tree_artifacts.ArchiveLimits(
        max_members=10,
        max_member_bytes=max_member_bytes,
        max_total_bytes=512,
        max_archive_bytes=1024,
    )


def test_wheel_members_are_bounded_and_mapped_to_policy_paths(tmp_path: Path) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/example.py", "safe\n")
        archive.writestr("fieldkit_cli-1.0.0.dist-info/METADATA", "Name: fieldkit-cli\n")

    evidence, documents = public_tree_artifacts.read_artifact(wheel, _limits())

    assert evidence.kind == "wheel"
    assert evidence.member_count == 2
    assert evidence.total_uncompressed_bytes == sum(len(document.data) for document in documents)
    assert len(evidence.sha256) == 64
    assert [(document.subject_path, document.policy_path) for document in documents] == [
        (f"artifact:{wheel.name}:fieldkit/example.py", "src/fieldkit/example.py"),
        (f"artifact:{wheel.name}:fieldkit_cli-1.0.0.dist-info/METADATA", "pyproject.toml"),
    ]


def test_wheel_digest_and_members_come_from_one_immutable_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/example.py", "original\n")
    original = wheel.read_bytes()
    zip_file = zipfile.ZipFile

    def replace_after_capture(source: io.BytesIO) -> zipfile.ZipFile:
        with zip_file(wheel, "w") as replacement:
            replacement.writestr("fieldkit/example.py", "replacement\n")
        return zip_file(source)

    monkeypatch.setattr(public_tree_artifacts.zipfile, "ZipFile", replace_after_capture)

    evidence, documents = public_tree_artifacts.read_artifact(wheel, _limits())

    assert evidence.sha256 == hashlib.sha256(original).hexdigest()
    assert documents[0].data == b"original\n"


def test_wheel_rejects_duplicate_member_names(tmp_path: Path) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/example.py", "first\n")
        archive.writestr("fieldkit/example.py", "second\n")

    with pytest.raises(ValueError, match="duplicate archive member"):
        public_tree_artifacts.read_artifact(wheel, _limits())


@pytest.mark.parametrize("name", ["../secret.txt", "/absolute.txt", "fieldkit\\escape.py"])
def test_wheel_rejects_noncanonical_member_paths(tmp_path: Path, name: str) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, "payload\n")

    with pytest.raises(ValueError, match="noncanonical archive member"):
        public_tree_artifacts.read_artifact(wheel, _limits())


def test_wheel_rejects_symlink_member(tmp_path: Path) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    link = zipfile.ZipInfo("fieldkit/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(link, "../../outside")

    with pytest.raises(ValueError, match="unsupported archive member type"):
        public_tree_artifacts.read_artifact(wheel, _limits())


def test_wheel_rejects_declared_oversized_member_before_reading(tmp_path: Path) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/large.txt", b"x" * 129)

    with pytest.raises(ValueError, match="member exceeds size limit"):
        public_tree_artifacts.read_artifact(wheel, _limits())


def test_sdist_rejects_link_and_accepts_one_canonical_root(tmp_path: Path) -> None:
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"safe\n"
        regular = tarfile.TarInfo("fieldkit_cli-1.0.0/src/fieldkit/example.py")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))
        link = tarfile.TarInfo("fieldkit_cli-1.0.0/src/fieldkit/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)

    with pytest.raises(ValueError, match="unsupported archive member type"):
        public_tree_artifacts.read_artifact(sdist, _limits())


def test_sdist_members_strip_exactly_one_root(tmp_path: Path) -> None:
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"safe\n"
        member = tarfile.TarInfo("fieldkit_cli-1.0.0/src/fieldkit/example.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        metadata = tarfile.TarInfo("fieldkit_cli-1.0.0/PKG-INFO")
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))

    evidence, documents = public_tree_artifacts.read_artifact(sdist, _limits())

    assert evidence.kind == "sdist"
    assert [document.policy_path for document in documents] == ["pyproject.toml", "src/fieldkit/example.py"]
