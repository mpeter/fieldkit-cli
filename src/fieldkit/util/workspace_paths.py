"""Canonical validation and resolution for workspace-relative output paths."""

from pathlib import Path


def validate_relative_output_path(relative_path: str) -> str:
    """Require unambiguous relative syntax before path resolution."""
    if any(character in relative_path for character in ("\\", ":", "\x00")) or any(
        part in {"", ".", ".."} for part in relative_path.split("/")
    ):
        raise ValueError("Invalid relative output path")
    return relative_path


def resolve_workspace_output(workspace: Path, relative_path: str) -> Path:
    """Resolve one exact relative destination without following child redirects.

    The configured workspace may be an alias. Its directory namespace must
    remain stable during publication; this is not protection against hostile
    same-user rename races. The workspace must already exist.
    """
    validate_relative_output_path(relative_path)
    try:
        root = workspace.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Cannot resolve prepared output path")
        candidate = root / relative_path
        target = candidate.resolve()
    except (OSError, RuntimeError):
        raise ValueError("Cannot resolve prepared output path") from None
    if not target.is_relative_to(root):
        raise ValueError("Prepared output escapes workspace")
    if target != candidate:
        raise ValueError("Prepared output redirects its authorized destination")
    return target
