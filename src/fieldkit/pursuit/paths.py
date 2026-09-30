"""Confined path resolution for one pursuit in the configured workspace."""

from pathlib import Path

from fieldkit.util.workspace_paths import resolve_workspace_output


class PursuitPathError(ValueError):
    """A pursuit selector did not resolve to a safe workspace file."""


def _relative_to_workspace(workspace: Path, candidate: Path) -> Path | None:
    """Return a lexical workspace-relative path without following the candidate."""
    try:
        bases = (workspace.absolute(), workspace.resolve(strict=True))
    except (OSError, RuntimeError):
        return None
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    for base in bases:
        try:
            return absolute.relative_to(base)
        except ValueError:
            continue
    return None


def _is_pursuit_namespace(relative: Path) -> bool:
    """Return whether a relative path names one account pursuit Markdown file."""
    parts = relative.parts
    return (
        len(parts) == 4
        and parts[0] == "accounts"
        and parts[2] == "pursuits"
        and parts[3].endswith(".md")
        and parts[3] != "template.md"
    )


def _confined_file(workspace: Path, candidate: Path) -> Path | None:
    relative = _relative_to_workspace(workspace, candidate)
    if relative is None or not _is_pursuit_namespace(relative):
        return None
    try:
        resolved = resolve_workspace_output(workspace, relative.as_posix())
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def resolve_pursuit_file(workspace: Path, spec: str) -> Path:
    """Resolve an absolute path or ``account/slug`` inside the pursuit namespace.

    Resolution never probes an outside path and rejects redirects at every child
    component. The configured workspace itself may be a filesystem alias.
    """
    candidate = Path(spec)
    direct = _confined_file(workspace, candidate)
    if direct is not None:
        return direct
    if not candidate.is_absolute():
        workspace_relative = _confined_file(workspace, workspace / candidate)
        if workspace_relative is not None:
            return workspace_relative

    parts = candidate.parts
    accounts_root = workspace / "accounts"
    if len(parts) == 3 and parts[1] == "pursuits":
        relative = _confined_file(workspace, accounts_root / candidate)
        if relative is not None:
            return relative
    if len(parts) == 2:
        account, slug = parts
        shorthand = _confined_file(workspace, accounts_root / account / "pursuits" / f"{slug}.md")
        if shorthand is not None:
            return shorthand

    raise PursuitPathError("Pursuit must be an existing workspace file or an account/slug selector")
