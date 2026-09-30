"""Capture a preparatory offline closure for installed-documentation scenarios."""

import os
import re
import sys
import sysconfig
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from scripts import documentation_dependencies as preparation
from scripts import release_filesystem
from scripts.artifact_limits import MAX_ARTIFACT_BYTES
from scripts.documentation_runtime import DependencyTarget
from scripts.release_consumer import (
    DownloadedArtifact,
    ExpectedArtifact,
    OfflineDependencies,
    validate_hashed_requirements,
)


@dataclass(frozen=True)
class VerifiedDependencies:
    """Private original bytes verified against an externally retained digest."""

    wheels: tuple[DownloadedArtifact, ...]
    runtime_requirements: DownloadedArtifact
    release_build_requirements: DownloadedArtifact

    def for_artifact(self, artifact: Path) -> OfflineDependencies:
        """Bind each freshly built distribution to its own original bytes."""
        kind: Literal["wheel", "sdist"]
        if artifact.name.endswith(".whl"):
            kind = "wheel"
        elif artifact.name.endswith(".tar.gz"):
            kind = "sdist"
        else:
            raise ValueError("documentation artifact must be a wheel or sdist")
        parent = release_filesystem.open_real_directory(artifact.parent)
        try:
            descriptor = os.open(artifact.name, release_filesystem.file_flags(), dir_fd=parent)
            try:
                data = release_filesystem.read_regular_file(descriptor, maximum_bytes=MAX_ARTIFACT_BYTES)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        expected = ExpectedArtifact(kind, artifact.name, sha256(data).hexdigest())
        return OfflineDependencies(expected, self.wheels, self.runtime_requirements, self.release_build_requirements)


@contextmanager
def verify_dependencies(root: Path, manifest_sha256: str, repo_root: Path) -> Iterator[VerifiedDependencies]:
    """Require the exact snapshot, interpreter and safe original dependency bytes.

    The supplied manifest retains preparatory authority only. Verified private
    copies are shared by both artifact journeys; no installer or acquisition
    runs here and no host configuration or cache is consulted.
    """
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("dependency manifest digest must be a full lowercase SHA-256")
    target: DependencyTarget = {
        "version": list(sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "platform": sysconfig.get_platform(),
    }
    requirements, wheels = preparation.capture_prepared_input(root, manifest_sha256, repo_root, target)
    with tempfile.TemporaryDirectory(prefix="fieldkit-documentation-dependencies-") as directory:
        private = Path(directory)
        (private / "wheels").mkdir()
        for name, data in wheels.items():
            (private / "wheels" / name).write_bytes(data)
        for name, data in requirements.items():
            path = private / name
            path.write_bytes(data)
            validate_hashed_requirements(path)
        yield VerifiedDependencies(
            tuple(
                DownloadedArtifact(private / "wheels" / name, sha256(data).hexdigest()) for name, data in wheels.items()
            ),
            DownloadedArtifact(
                private / "runtime-requirements.txt", sha256(requirements["runtime-requirements.txt"]).hexdigest()
            ),
            DownloadedArtifact(
                private / "release-build-requirements.txt",
                sha256(requirements["release-build-requirements.txt"]).hexdigest(),
            ),
        )
