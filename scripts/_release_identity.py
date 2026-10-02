"""Shared stable release identity checks, independent of publication authorization."""

from __future__ import annotations

import re

STABLE_VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def tag_version(tag: object) -> str:
    """Return the exact stable version selected by a canonical release tag."""
    if not isinstance(tag, str) or re.fullmatch(r"v" + STABLE_VERSION_PATTERN, tag) is None:
        raise ValueError("planned tag must be a canonical stable SemVer tag")
    return tag[1:]


def validate_artifact_identity(name: str, kind: str, package: str, tag: object) -> None:
    """Reject stale or foreign filenames before accepting their digest evidence."""
    version = tag_version(tag)
    distribution = re.sub(r"[-_.]+", "_", package).lower()
    prefix = f"{distribution}-{version}"
    if kind == "sdist":
        valid = name == f"{prefix}.tar.gz"
    elif kind == "wheel":
        valid = re.fullmatch(re.escape(prefix) + r"-(?:[0-9][^-]*-)?[^-]+-[^-]+-[^-]+\.whl", name) is not None
    else:
        valid = False
    if not valid:
        raise ValueError("artifact filename does not bind the candidate package and planned tag")
