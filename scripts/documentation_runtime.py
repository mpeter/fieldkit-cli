"""Held tool descriptors shared by the trusted documentation supervisor."""

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


@dataclass(frozen=True)
class RuntimeTools:
    """Descriptors whose owners keep them open across every sandbox run."""

    uv_descriptor: int
    python_descriptor: int


@dataclass(frozen=True)
class DependencyInput:
    """Held preparatory dependency directory and externally retained manifest digest."""

    directory_descriptor: int
    manifest_sha256: str


def directory_options(path: Path) -> list[str]:
    """Create only the empty parent chain needed by one exact mount."""
    options: list[str] = []
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        options.extend(("--dir", str(current)))
    return options


class DependencyTarget(TypedDict):
    """Interpreter and platform identity bound into a prepared wheel closure."""

    version: list[int]
    implementation: str
    cache_tag: str | None
    platform: str
