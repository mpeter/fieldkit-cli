"""Extract one GitHub release-input artifact ZIP through fixed safety bounds."""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 256


def _member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name.rstrip("/"))
    if not name or not path.parts or path.is_absolute() or "\\" in name or "\0" in name or ".." in path.parts:
        raise ValueError(f"approval archive member path is unsafe: {name!r}")
    if path.as_posix() != name.rstrip("/"):
        raise ValueError(f"approval archive member path is noncanonical: {name!r}")
    return path


def _check_archive(archive: Path) -> list[zipfile.ZipInfo]:
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("approval archive must be a regular file")
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("approval archive exceeds compressed-size limit")
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
    except zipfile.BadZipFile as error:
        raise ValueError("approval archive is not a valid ZIP file") from error
    if not members or len(members) > _MAX_MEMBERS:
        raise ValueError("approval archive member count is invalid")
    paths: set[PurePosixPath] = set()
    total = 0
    for member in members:
        path = _member_path(member.filename)
        if path in paths:
            raise ValueError("approval archive has duplicate members")
        paths.add(path)
        mode = member.external_attr >> 16
        member_type = stat.S_IFMT(mode)
        if member.is_dir():
            if member_type not in {0, stat.S_IFDIR} or member.file_size:
                raise ValueError("approval archive has an unsupported directory member")
            continue
        if member_type not in {0, stat.S_IFREG} or member.flag_bits & 0x1:
            raise ValueError("approval archive has an unsupported file member")
        if member.file_size < 0 or member.file_size > _MAX_MEMBER_BYTES:
            raise ValueError("approval archive member exceeds size limit")
        total += member.file_size
        if total > _MAX_TOTAL_BYTES:
            raise ValueError("approval archive exceeds uncompressed-size limit")
    return members


def extract(archive: Path, destination: Path) -> None:
    """Safely extract one bounded artifact archive into a new destination directory."""
    members = _check_archive(archive)
    if destination.exists() or destination.is_symlink():
        raise ValueError("approval archive destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        with zipfile.ZipFile(archive) as source:
            for member in members:
                relative = _member_path(member.filename)
                target = staging.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as input_stream, target.open("xb") as output_stream:
                    remaining = member.file_size
                    while remaining:
                        chunk = input_stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError(f"approval archive member is truncated: {member.filename}")
                        output_stream.write(chunk)
                        remaining -= len(chunk)
                    if input_stream.read(1):
                        raise ValueError(f"approval archive member is oversized: {member.filename}")
                target.chmod(0o600)
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        extract(args.archive, args.destination)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Release approval archive: ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Release approval archive: PASS ({args.destination})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
