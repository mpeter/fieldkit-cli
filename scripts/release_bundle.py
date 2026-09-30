"""Materialize and verify the closed local release bundle for one candidate."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from argparse import ArgumentParser
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar, overload

if TYPE_CHECKING:
    from scripts._public_history_bundle import PreparationBundleReport, PreparedCandidate
    from scripts.public_history_source import ApprovedCutoverAnchor

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import release_filesystem
from scripts._release_bundle_evidence import (
    _BUNDLE_MEMBER_COUNT,
    _BUNDLE_SCHEMA_VERSION,
    _CHECKSUMS_NAME,
    _DIGEST_LENGTH,
    _MAX_FILE_BYTES,
    _PROVENANCE_NAME,
    _RELEASE_BUILD_REQUIREMENTS_NAME,
    _RUNTIME_REQUIREMENTS_NAME,
    _RUNTIME_WHEELHOUSE_NAME,
    _SBOM_NAME,
    MAX_WHEELHOUSE_BYTES,
    _basename,
    _candidate_inputs,
    _digest,
    _digest_string,
    _json_object,
    _object,
    _provenance,
    _verify_candidate_binding,
    _verify_provenance,
)
from scripts.runtime_license_inventory import (
    MAX_OBSERVATION_BYTES,
    OBSERVATIONS_NAME,
    PLATFORM_REQUIREMENTS_NAME,
    normalize_license_expression,
    package_url,
    parse_observations,
)

_BUNDLE_DIRECTORY = "bundle"
_REJECTED_CHECKSUMS_NAME = "rejected-SHA256SUMS"
_REJECTION_NAME = "bundle-rejection.txt"


@dataclass(frozen=True)
class BundleReport:
    """Successful verification evidence for one closed local release bundle."""

    source_commit: str
    files: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        """Return whether this report represents a successful verification."""
        return True

    def to_dict(self) -> dict[str, object]:
        """Render stable machine-readable bundle verification evidence."""
        return {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "status": "pass",
            "source_commit": self.source_commit,
            "files": [{"name": name, "sha256": digest} for name, digest in self.files],
        }


_ReportT = TypeVar("_ReportT", bound="BundleReport | PreparationBundleReport", covariant=True)


@dataclass(frozen=True)
class VerifiedBundle(Generic[_ReportT]):
    """Verification evidence and the exact member bytes checked to produce it."""

    report: _ReportT
    members: Mapping[str, bytes]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", MappingProxyType(dict(self.members)))


def _read_regular_file(descriptor: int, subject: str, *, maximum_bytes: int = _MAX_FILE_BYTES) -> bytes:
    """Read a bounded input with bundle-specific diagnostics."""
    try:
        return release_filesystem.read_regular_file(descriptor, maximum_bytes=maximum_bytes)
    except ValueError as error:
        raise ValueError(f"bundle {error}: {subject}") from error


def _safe_bytes(path: Path) -> bytes:
    """Read one bounded regular file without accepting a symlink."""
    try:
        descriptor = os.open(path, release_filesystem.file_flags())
    except OSError as error:
        raise ValueError(f"bundle input is unavailable: {path.name}") from error
    try:
        return _read_regular_file(descriptor, path.name)
    finally:
        os.close(descriptor)


def _safe_bytes_at(directory_fd: int, name: str, *, maximum_bytes: int = _MAX_FILE_BYTES) -> bytes:
    """Read one bounded regular directory member through an open root descriptor."""
    try:
        descriptor = os.open(name, release_filesystem.file_flags(), dir_fd=directory_fd)
    except OSError as error:
        raise ValueError(f"bundle input is unavailable: {name}") from error
    try:
        return _read_regular_file(descriptor, name, maximum_bytes=maximum_bytes)
    finally:
        os.close(descriptor)


def _write_bytes_at(directory_fd: int, name: str, data: bytes) -> None:
    """Write one staged bundle file relative to a pinned directory."""
    os.close(_write_retained_bytes_at(directory_fd, name, data))


def _write_retained_bytes_at(directory_fd: int, name: str, data: bytes) -> int:
    """Write through one new file descriptor and retain that exact inode."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _checksum_manifest(files: tuple[tuple[str, str], ...]) -> bytes:
    """Render the canonical digest list for all non-manifest bundle files."""
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(files)).encode("utf-8")


def _open_directory(path: Path) -> int:
    """Open a directory with bundle-specific diagnostics."""
    try:
        return release_filesystem.open_directory(path)
    except OSError as error:
        raise ValueError("bundle directory is unavailable") from error
    except ValueError as error:
        raise ValueError("bundle path must be a directory") from error


def _open_directory_at(parent_fd: int, name: str) -> int:
    """Open a child directory with bundle-specific diagnostics."""
    try:
        return release_filesystem.open_directory_at(parent_fd, name)
    except OSError as error:
        raise ValueError(f"bundle directory is unavailable: {name}") from error
    except ValueError as error:
        raise ValueError(f"bundle path must be a directory: {name}") from error


def _create_staging_directory(parent_fd: int) -> tuple[str, int]:
    for _attempt in range(100):
        name = f".{_BUNDLE_DIRECTORY}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name, _open_directory_at(parent_fd, name)
    raise ValueError("cannot allocate release bundle staging directory")


def _rename_no_replace_at(parent_fd: int, source: str, destination: str) -> None:
    """Translate publication failures into this consumer's diagnostics."""
    try:
        release_filesystem.rename_no_replace_at(parent_fd, source, destination)
    except FileExistsError as error:
        raise ValueError("candidate bundle already exists") from error
    except ValueError as error:
        raise ValueError("atomic release bundle publication is unavailable or unsupported") from error


@overload
def materialize(
    candidate: Path, *, expected_anchor: None = None, cutover_record: None = None, source_repo: None = None
) -> BundleReport: ...


@overload
def materialize(
    candidate: Path, *, expected_anchor: ApprovedCutoverAnchor, cutover_record: bytes, source_repo: Path
) -> PreparationBundleReport: ...


@overload
def materialize(
    candidate: Path,
    *,
    expected_anchor: ApprovedCutoverAnchor | None,
    cutover_record: bytes | None,
    source_repo: Path | None = None,
) -> BundleReport | PreparationBundleReport: ...


def materialize(
    candidate: Path,
    *,
    expected_anchor: ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
    source_repo: Path | None = None,
) -> BundleReport | PreparationBundleReport:
    """Create a closed verified release bundle from one successful candidate.

    Successor retention handlers suppress standard formatted exception chains,
    not the exception objects' retained context. Callers must never export those
    private exception objects as diagnostics. Arbitrary asynchronous interrupts,
    including handler and final descriptor-close windows, can bypass retention
    or suppression; this is not a cancellation-timing guarantee.
    """
    if source_repo is not None and expected_anchor is None:
        raise ValueError("initial export cannot accept successor source trust")
    if (expected_anchor is None) != (cutover_record is None):
        raise ValueError("successor bundle requires independent anchor and exact cutover record")
    if expected_anchor is not None and source_repo is None:
        raise ValueError("successor producer requires a trusted current source repository")
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("candidate must be a directory")
    candidate_fd = _open_directory(candidate)
    checksum_fd: int | None = None
    staging_name: str | None = None
    staging_fd: int | None = None
    published = False
    prepared = None
    successor_source_files: tuple[tuple[bytes, str, str], ...] = ()
    successor_provenance: bytes | None = None
    try:
        try:
            os.stat(_BUNDLE_DIRECTORY, dir_fd=candidate_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("candidate bundle already exists")
        report_bytes = _safe_bytes_at(candidate_fd, "report.json")
        if expected_anchor is not None or cutover_record is not None:
            if expected_anchor is None or cutover_record is None:
                raise ValueError("successor bundle requires independent anchor and exact cutover record")
            from scripts import _public_history_bundle, public_history_source

            prepared = _public_history_bundle.candidate_inputs(
                report_bytes,
                _safe_bytes_at(
                    candidate_fd,
                    "manifest.json",
                    maximum_bytes=public_history_source.MAX_SOURCE_BYTES,
                ),
                expected_anchor=expected_anchor,
                cutover_record=cutover_record,
            )
            _verify_live_source(candidate, candidate_fd, source_repo, prepared, expected_anchor, cutover_record)
            successor_source_files = (
                (prepared.source_bytes, _public_history_bundle.SOURCE_NAME, _digest(prepared.source_bytes)),
            )
            successor_provenance = _public_history_bundle.provenance(prepared, _digest(report_bytes))
        inputs = _candidate_inputs(report_bytes) if prepared is None else prepared.inputs
        dist_fd = _open_directory_at(candidate_fd, "dist")
        license_evidence = _object(inputs.report["license_evidence"], "candidate license evidence")
        try:
            assets = (
                (
                    OBSERVATIONS_NAME,
                    _object(license_evidence["observations"], "candidate license observations")["sha256"],
                    MAX_OBSERVATION_BYTES,
                ),
                (
                    PLATFORM_REQUIREMENTS_NAME,
                    _object(license_evidence["platform_requirements"], "candidate platform requirements")["sha256"],
                    _MAX_FILE_BYTES,
                ),
                (_SBOM_NAME, inputs.sbom_sha256, _MAX_FILE_BYTES),
                (_RUNTIME_REQUIREMENTS_NAME, inputs.runtime_requirements_sha256, _MAX_FILE_BYTES),
                (_RELEASE_BUILD_REQUIREMENTS_NAME, inputs.release_build_requirements_sha256, _MAX_FILE_BYTES),
                (_RUNTIME_WHEELHOUSE_NAME, inputs.runtime_wheelhouse_sha256, MAX_WHEELHOUSE_BYTES),
            )
            source_files = (
                tuple(
                    (_safe_bytes_at(candidate_fd, name, maximum_bytes=limit), name, digest)
                    for name, digest, limit in assets
                )
                + tuple((_safe_bytes_at(dist_fd, name), name, digest) for name, digest in inputs.artifacts)
                + successor_source_files
            )
        finally:
            os.close(dist_fd)
        candidate_report_digest = _digest(report_bytes)
        for data, name, expected_digest in source_files:
            if _digest(data) != expected_digest:
                if name == _SBOM_NAME:
                    subject = "SBOM"
                elif name == _RUNTIME_REQUIREMENTS_NAME:
                    subject = "runtime requirements"
                elif name == _RELEASE_BUILD_REQUIREMENTS_NAME:
                    subject = "release build requirements"
                elif name == OBSERVATIONS_NAME:
                    subject = "license observations"
                elif name == PLATFORM_REQUIREMENTS_NAME:
                    subject = "platform requirements"
                elif name == _RUNTIME_WHEELHOUSE_NAME:
                    subject = "runtime wheelhouse"
                elif successor_source_files and name == successor_source_files[0][1]:
                    subject = "source contract"
                else:
                    subject = "artifact"
                raise ValueError(f"candidate {subject} digest does not match: {name}")
        staging_name, staging_fd = _create_staging_directory(candidate_fd)
        bundle_files = tuple(
            (name, _digest_string(digest, "candidate file digest")) for _, name, digest in source_files
        )
        provenance = (
            _provenance(inputs, candidate_report_digest) if successor_provenance is None else successor_provenance
        )
        all_files = tuple(sorted((*bundle_files, (_PROVENANCE_NAME, _digest(provenance)))))
        for data, name, _ in source_files:
            _write_bytes_at(staging_fd, name, data)
        _write_bytes_at(staging_fd, _PROVENANCE_NAME, provenance)
        if prepared is None:
            _write_bytes_at(staging_fd, _CHECKSUMS_NAME, _checksum_manifest(all_files))
        else:
            checksum_fd = _write_retained_bytes_at(staging_fd, _CHECKSUMS_NAME, _checksum_manifest(all_files))
        _verify_open_bundle(
            staging_fd, candidate_report=report_bytes, expected_anchor=expected_anchor, cutover_record=cutover_record
        )
        if _safe_bytes_at(candidate_fd, "report.json") != report_bytes:
            raise ValueError("candidate report changed during bundle preparation")
        if prepared is not None:
            _verify_live_source(candidate, candidate_fd, source_repo, prepared, expected_anchor, cutover_record)
        _rename_no_replace_at(candidate_fd, staging_name, _BUNDLE_DIRECTORY)
        published = True
        published_fd = _open_directory_at(candidate_fd, _BUNDLE_DIRECTORY)
        try:
            staged = os.fstat(staging_fd)
            published_stat = os.fstat(published_fd)
            identity = (staged.st_dev, staged.st_ino)
            if (published_stat.st_dev, published_stat.st_ino) != identity:
                raise ValueError("published bundle identity does not match retained staging")
            verification = _verify_open_bundle(
                published_fd,
                candidate_report=report_bytes,
                expected_anchor=expected_anchor,
                cutover_record=cutover_record,
            )
            final = os.stat(_BUNDLE_DIRECTORY, dir_fd=candidate_fd, follow_symlinks=False)
            if not stat.S_ISDIR(final.st_mode) or (final.st_dev, final.st_ino) != identity:
                raise ValueError("published bundle identity changed during verification")
            if _safe_bytes_at(candidate_fd, "report.json") != report_bytes:
                raise ValueError("candidate report changed during bundle preparation")
            if prepared is not None:
                _verify_live_source(candidate, candidate_fd, source_repo, prepared, expected_anchor, cutover_record)
            return verification
        finally:
            os.close(published_fd)
    except BaseException as error:
        if prepared is not None and staging_fd is not None and staging_name is not None:
            try:
                _reject_successor_bundle(
                    candidate_fd,
                    staging_fd,
                    _BUNDLE_DIRECTORY if published else staging_name,
                    checksum_fd=checksum_fd,
                )
            except Exception:  # noqa: BLE001 -- retain original cancellation and sanitize any retention failure
                # Retention failure must not expose the original filesystem/FFI diagnostic.
                if not isinstance(error, Exception):
                    error.add_note("successor rejected bundle retention failed")
                    raise error from None
                raise ValueError("successor rejected bundle retention failed") from None
            except BaseException as interruption:
                interruption.add_note("successor rejected bundle retention interrupted")
                raise interruption from None
            if isinstance(error, Exception) and not isinstance(error, ValueError):
                raise ValueError("successor bundle preparation failed") from None
            raise error from None
        raise
    finally:
        if checksum_fd is not None:
            os.close(checksum_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(candidate_fd)


def _reject_successor_bundle(parent_fd: int, retained_fd: int, name: str, *, checksum_fd: int | None) -> None:
    """Invalidate retained bytes before forensic quarantine, not just its name.

    This retains local rejection evidence; it does not authenticate bytes against
    an actor deliberately reconstructing a previous valid preparation bundle.
    If all filesystem mutations fail, only a sanitized failure can be reported.
    """
    failed = False
    try:
        _rename_no_replace_at(retained_fd, _CHECKSUMS_NAME, _REJECTED_CHECKSUMS_NAME)
    except Exception as error:  # noqa: BLE001 -- every independent invalidation attempt must continue
        failed = checksum_fd is not None or not isinstance(error, FileNotFoundError)
        if checksum_fd is not None:
            try:
                os.ftruncate(checksum_fd, 0)
            except Exception:  # noqa: BLE001 -- report failure after remaining retention attempts
                # Rename already failed, so final failure remains mandatory.
                failed = True
    try:
        _write_bytes_at(retained_fd, _REJECTION_NAME, b"preparation rejected\n")
    except Exception:  # noqa: BLE001 -- quarantine must still be attempted after marker failure
        failed = True
    try:
        _quarantine_rejected_bundle(parent_fd, retained_fd, name)
    except Exception:  # noqa: BLE001 -- sanitize unexpected filesystem/FFI errors after all attempts
        failed = True
    if failed:
        raise ValueError("successor rejected bundle retention failed") from None


def _quarantine_rejected_bundle(parent_fd: int, retained_fd: int, name: str) -> None:
    """Move only the retained invalidated directory, without replacing a path."""
    retained = os.fstat(retained_fd)
    identity = (retained.st_dev, retained.st_ino)
    matched = None
    for candidate_name in dict.fromkeys((name, _BUNDLE_DIRECTORY)):
        try:
            current = os.stat(candidate_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            matched = candidate_name
            break
    if matched is None:
        raise ValueError("rejected bundle directory identity changed")
    quarantine = f".bundle-rejected-{secrets.token_hex(16)}"
    _rename_no_replace_at(parent_fd, matched, quarantine)
    moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(moved.st_mode) or (moved.st_dev, moved.st_ino) != identity:
        raise ValueError("rejected bundle quarantine identity changed")


def _verify_live_source(
    candidate: Path,
    descriptor: int,
    repo: Path | None,
    prepared: PreparedCandidate,
    anchor: ApprovedCutoverAnchor | None,
    record: bytes | None,
) -> None:
    """Verify real current producer source; offline receipts cannot replace this."""
    if repo is None or anchor is None or record is None:
        raise ValueError("successor producer requires a trusted current source repository")
    from scripts import public_history_source, public_tree_scan

    retained = os.fstat(descriptor)
    current = candidate.stat(follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (retained.st_dev, retained.st_ino):
        raise ValueError("successor candidate directory identity changed")
    public_history_source.verify_source(
        repo, prepared.source, candidate / "export", expected_anchor=anchor, cutover_record=record
    )
    entries = {entry.path: entry for entry in prepared.source.entries}
    scan = _object(prepared.inputs.report["scan"], "candidate scan")
    for field, path in (
        ("scan_policy_sha256", public_tree_scan.POLICY_PATH.as_posix()),
        ("identity_policy_sha256", public_tree_scan.IDENTITY_POLICY_PATH.as_posix()),
    ):
        if _digest(public_tree_scan._source_document(candidate / "export", entries[path])) != scan[field]:
            raise ValueError("successor scan policy digest differs from verified source bytes")


def _parse_checksums(data: bytes) -> tuple[tuple[str, str], ...]:
    """Require the canonical closed checksum manifest grammar."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("bundle checksum manifest is not UTF-8") from error
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("bundle checksum manifest is not canonical")
    lines = text.removesuffix("\n").split("\n")
    entries: list[tuple[str, str]] = []
    for line in lines:
        if len(line) < _DIGEST_LENGTH + 3 or line[_DIGEST_LENGTH : _DIGEST_LENGTH + 2] != "  ":
            raise ValueError("bundle checksum manifest has an invalid line")
        entries.append(
            (
                _basename(line[_DIGEST_LENGTH + 2 :], "bundle checksum filename"),
                _digest_string(line[:_DIGEST_LENGTH], "bundle checksum digest"),
            )
        )
    if not entries or entries != sorted(entries) or len({name for name, _ in entries}) != len(entries):
        raise ValueError("bundle checksum manifest is not canonical")
    return tuple(entries)


@overload
def _verify_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: None = None,
    cutover_record: None = None,
) -> BundleReport: ...


@overload
def _verify_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None,
    cutover_record: bytes | None,
) -> BundleReport | PreparationBundleReport: ...


def _verify_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
) -> BundleReport | PreparationBundleReport:
    """Verify a release bundle already pinned to an open directory descriptor."""
    return capture_open_bundle(
        directory_fd,
        candidate_report=candidate_report,
        expected_checksums=expected_checksums,
        expected_anchor=expected_anchor,
        cutover_record=cutover_record,
    ).report


@overload
def capture_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: None = None,
    cutover_record: None = None,
) -> VerifiedBundle[BundleReport]: ...


@overload
def capture_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> VerifiedBundle[PreparationBundleReport]: ...


@overload
def capture_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None,
    cutover_record: bytes | None,
) -> VerifiedBundle[BundleReport | PreparationBundleReport]: ...


def capture_open_bundle(
    directory_fd: int,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
) -> VerifiedBundle[BundleReport | PreparationBundleReport]:
    """Verify once and retain immutable member bytes from the pinned directory."""
    if (expected_anchor is None) != (cutover_record is None):
        raise ValueError("successor bundle requires independent anchor and exact cutover record")
    source_limit = _MAX_FILE_BYTES
    source_name: str | None = None
    successor_verifier = None
    if expected_anchor is not None:
        from scripts import _public_history_bundle, public_history_source

        source_limit = public_history_source.MAX_SOURCE_BYTES
        source_name = _public_history_bundle.SOURCE_NAME
        successor_verifier = _public_history_bundle
    contents_list: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if entry.name in {_REJECTED_CHECKSUMS_NAME, _REJECTION_NAME}:
                raise ValueError("bundle preparation was rejected")
            if len(contents_list) == _BUNDLE_MEMBER_COUNT + (source_name is not None):
                raise ValueError("bundle has unexpected or missing files")
            contents_list.append(entry.name)
    contents = tuple(contents_list)
    data = {
        name: _safe_bytes_at(
            directory_fd,
            name,
            maximum_bytes=(
                MAX_WHEELHOUSE_BYTES
                if name == _RUNTIME_WHEELHOUSE_NAME
                else source_limit
                if name == source_name
                else MAX_OBSERVATION_BYTES
                if name == OBSERVATIONS_NAME
                else _MAX_FILE_BYTES
            ),
        )
        for name in contents
    }
    if _CHECKSUMS_NAME not in data:
        raise ValueError("bundle has unexpected or missing files")
    if expected_checksums is not None and data[_CHECKSUMS_NAME] != expected_checksums:
        raise ValueError("bundle checksums do not match the outer approval manifest")
    checksums = _parse_checksums(data[_CHECKSUMS_NAME])
    expected_contents = {name for name, _ in checksums} | {_CHECKSUMS_NAME}
    if set(contents) != expected_contents:
        raise ValueError("bundle has unexpected or missing files")
    for name, expected_digest in checksums:
        if _digest(data[name]) != expected_digest:
            raise ValueError(f"bundle digest does not match: {name}")
    if _PROVENANCE_NAME not in data:
        raise ValueError("bundle has unexpected or missing files")
    prepared_report = None
    if successor_verifier is not None:
        if expected_anchor is None or cutover_record is None:
            raise ValueError("successor bundle requires independent anchor and exact cutover record")
        if successor_verifier.SOURCE_NAME not in data:
            raise ValueError("successor bundle is missing its complete source contract")
        prepared = successor_verifier.verify(
            data[_PROVENANCE_NAME],
            checksums,
            candidate_report,
            data[successor_verifier.SOURCE_NAME],
            expected_anchor=expected_anchor,
            cutover_record=cutover_record,
        )
        inputs = prepared.inputs
        prepared_report = successor_verifier.PreparationBundleReport(prepared.source.source_commit, checksums)
        source_commit = prepared_report.source_commit
    else:
        provenance = _json_object(data[_PROVENANCE_NAME], "bundle provenance")
        source_commit = _verify_provenance(provenance, checksums)
        inputs = _verify_candidate_binding(provenance, candidate_report)
    raw_packages, marker_environment = parse_observations(data[OBSERVATIONS_NAME])
    receipt = _object(inputs.report["license_evidence"], "candidate license evidence")
    normalized_packages = sorted(
        (
            {
                "package_url": package_url(row["name"], row["version"]),
                "license_expression": normalize_license_expression(row["license_expression"]),
            }
            for row in raw_packages
        ),
        key=lambda row: row["package_url"],
    )
    if normalized_packages != receipt["packages"] or marker_environment != receipt["marker_environment"]:
        raise ValueError("bundle raw license observations do not match the candidate license evidence")
    if not data[PLATFORM_REQUIREMENTS_NAME].strip():
        raise ValueError("bundle platform requirements must be non-empty")
    report = BundleReport(source_commit, checksums) if prepared_report is None else prepared_report
    return VerifiedBundle(report, data)


@overload
def verify(
    bundle: Path,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: None = None,
    cutover_record: None = None,
) -> BundleReport: ...


@overload
def verify(
    bundle: Path,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> PreparationBundleReport: ...


@overload
def verify(
    bundle: Path,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None,
    cutover_record: bytes | None,
) -> BundleReport | PreparationBundleReport: ...


def verify(
    bundle: Path,
    *,
    candidate_report: bytes,
    expected_checksums: bytes | None = None,
    expected_anchor: ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
) -> BundleReport | PreparationBundleReport:
    """Fail closed unless a local release bundle is complete and digest-bound."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("bundle must be a directory")
    try:
        directory_fd = _open_directory(bundle)
    except ValueError as error:
        raise ValueError("bundle is unavailable") from error
    try:
        return _verify_open_bundle(
            directory_fd,
            candidate_report=candidate_report,
            expected_checksums=expected_checksums,
            expected_anchor=expected_anchor,
            cutover_record=cutover_record,
        )
    finally:
        os.close(directory_fd)


def _parser() -> ArgumentParser:
    """Build the standalone local bundle-verification command parser."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Verify one bundle and emit stable local verification evidence."""
    try:
        args = _parser().parse_args(argv)
        report = verify(args.bundle, candidate_report=_safe_bytes(args.candidate_report))
        if args.as_json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Release bundle: PASS ({report.source_commit})")
    except ValueError as error:
        print(f"Release bundle: ERROR: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
