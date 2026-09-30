"""Pure parsing and validation for revision-bound driver change authority."""

from pathlib import PurePosixPath

_MAX_DIFF_BYTES = 2 * 1024 * 1024
_STATUSES = frozenset("ACDMRTUXB")


class ChangeAuthorityError(RuntimeError):
    """A submitted Git diff is malformed, unsafe, or outside declared authority."""


def _changed_paths(diff_output: bytes) -> tuple[str, ...]:
    if len(diff_output) > _MAX_DIFF_BYTES:
        raise ChangeAuthorityError("submitted change list exceeds its byte limit")
    records = diff_output.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    paths: list[str] = []
    index = 0
    try:
        while index < len(records):
            status = records[index].decode("ascii")
            index += 1
            path_count = 2 if status.startswith(("R", "C")) else 1
            if not status or status[0] not in _STATUSES or index + path_count > len(records):
                raise ChangeAuthorityError("submitted change list is malformed")
            for raw_path in records[index : index + path_count]:
                path = raw_path.decode("utf-8")
                parsed = PurePosixPath(path)
                if not path or path.startswith("/") or "\\" in path or ".." in parsed.parts:
                    raise ChangeAuthorityError("submitted change path is unsafe")
                paths.append(parsed.as_posix())
            index += path_count
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChangeAuthorityError("submitted change list is malformed") from exc
    return tuple(paths)


def validate_change_authority(diff_output: bytes, covers: frozenset[str] | None) -> None:
    """Reject any changed path not contained by the exhaustive covers set."""
    if covers is None:
        raise ChangeAuthorityError("driver contract has no declared covers")
    if any(
        not any(path == cover or (cover.endswith("/") and path.startswith(cover)) for cover in covers)
        for path in _changed_paths(diff_output)
    ):
        raise ChangeAuthorityError("submitted changes exceed declared covers")
