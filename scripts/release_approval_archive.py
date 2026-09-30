"""Extract one GitHub release-input artifact ZIP through fixed safety bounds."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts import release_filesystem
else:
    import release_filesystem

_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 256


def _open_directory_at(parent_fd: int, name: str) -> int:
    try:
        return release_filesystem.open_directory_at(parent_fd, name)
    except ValueError as error:
        raise ValueError("approval archive path component must be a directory") from error


def _open_real_directory(path: Path, *, create: bool) -> int:
    """Open a directory by traversing real components from the filesystem root."""
    absolute = path.absolute()
    descriptor = release_filesystem.open_directory(Path(absolute.anchor))
    try:
        for part in absolute.parts[1:]:
            try:
                child = _open_directory_at(descriptor, part)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, (OSError, ValueError)):
            raise ValueError("approval archive paths must have only real path components") from error
        raise


def _open_archive(path: Path) -> int:
    if not path.name:
        raise ValueError("approval archive must be a regular file")
    parent_fd = _open_real_directory(path.parent, create=False)
    try:
        flags = release_filesystem.file_flags()
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ValueError("approval archive must be a regular file") from error
    finally:
        os.close(parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("approval archive must be a regular file")
        if metadata.st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("approval archive exceeds compressed-size limit")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name.rstrip("/"))
    if not name or not path.parts or path.is_absolute() or "\\" in name or "\0" in name or ".." in path.parts:
        raise ValueError("approval archive member path is unsafe")
    if path.as_posix() != name.rstrip("/"):
        raise ValueError("approval archive member path is noncanonical")
    return path


def _check_archive(source: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = source.infolist()
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


def _create_staging_directory(parent_fd: int, destination_name: str) -> tuple[str, int]:
    for _attempt in range(100):
        name = f".{destination_name}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name, _open_directory_at(parent_fd, name)
    raise ValueError("cannot allocate approval archive staging directory")


def _open_or_create_member_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child = _open_directory_at(descriptor, part)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rename_no_replace_at(parent_fd: int, source: str, destination: str) -> None:
    """Translate publication failures into this consumer's diagnostics."""
    try:
        release_filesystem.rename_no_replace_at(parent_fd, source, destination)
    except FileExistsError as error:
        raise ValueError("approval archive destination must not already exist") from error
    except ValueError as error:
        raise ValueError("atomic approval archive publication is unavailable or unsupported") from error


def extract(archive: Path, destination: Path) -> None:
    """Safely extract one bounded artifact archive into a new destination directory."""
    if not destination.name:
        raise ValueError("approval archive destination must be a named directory")
    archive_fd = _open_archive(archive)
    try:
        with os.fdopen(archive_fd, "rb") as archive_stream, zipfile.ZipFile(archive_stream) as source:
            members = _check_archive(source)
            parent_fd = _open_real_directory(destination.parent, create=True)
            staging_name: str | None = None
            staging_fd: int | None = None
            try:
                try:
                    os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("approval archive destination must not already exist")
                staging_name, staging_fd = _create_staging_directory(parent_fd, destination.name)
                for member in members:
                    relative = _member_path(member.filename)
                    directory_parts = relative.parts if member.is_dir() else relative.parts[:-1]
                    directory_fd = _open_or_create_member_directory(staging_fd, directory_parts)
                    try:
                        if member.is_dir():
                            continue
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                        output_fd = os.open(relative.parts[-1], flags, 0o600, dir_fd=directory_fd)
                        with os.fdopen(output_fd, "wb") as output_stream, source.open(member) as input_stream:
                            remaining = member.file_size
                            while remaining:
                                chunk = input_stream.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("approval archive member is truncated")
                                output_stream.write(chunk)
                                remaining -= len(chunk)
                            if input_stream.read(1):
                                raise ValueError("approval archive member is oversized")
                    finally:
                        os.close(directory_fd)
                _rename_no_replace_at(parent_fd, staging_name, destination.name)
                published_fd = _open_directory_at(parent_fd, destination.name)
                try:
                    staged = os.fstat(staging_fd)
                    published = os.fstat(published_fd)
                    identity = (staged.st_dev, staged.st_ino)
                    if (published.st_dev, published.st_ino) != identity:
                        raise ValueError("published approval archive identity does not match retained staging")
                    final = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(final.st_mode) or (final.st_dev, final.st_ino) != identity:
                        raise ValueError("published approval archive identity changed before completion")
                finally:
                    os.close(published_fd)
            finally:
                if staging_fd is not None:
                    os.close(staging_fd)
                os.close(parent_fd)
    except zipfile.BadZipFile as error:
        raise ValueError("approval archive is not a valid ZIP file") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        extract(args.archive, args.destination)
    except OSError:
        print("Release approval archive: ERROR: approval archive filesystem operation failed", file=sys.stderr)
        return 2
    except (ValueError, zipfile.BadZipFile) as error:
        print(f"Release approval archive: ERROR: {error}", file=sys.stderr)
        return 2
    print("Release approval archive: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
