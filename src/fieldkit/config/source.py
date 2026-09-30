"""Recognize a fieldkit source tree without guessing installed-package roots."""

import tomllib
from pathlib import Path

from fieldkit.util.text_snapshot import read_text_snapshot

MAX_PROJECT_METADATA_BYTES = 64 * 1024


def discover_source_checkout(module_file: Path) -> Path | None:
    """Return the identified source root, or None for an installed package.

    Recognition requires the loaded module to belong to the exact
    ``src/fieldkit`` layout and the same tree to identify as fieldkit-cli.
    This is source-layout discovery, not repository or release attestation.
    """
    try:
        module = module_file.resolve(strict=True)
        package = next(
            (parent for parent in module.parents if parent.name == "fieldkit" and parent.parent.name == "src"),
            None,
        )
        if package is None or not (package / "__main__.py").is_file():
            return None
        root = package.parent.parent
        metadata = tomllib.loads(
            read_text_snapshot(root / "pyproject.toml", max_bytes=MAX_PROJECT_METADATA_BYTES).content
        )
    except (OSError, RuntimeError, ValueError):
        return None
    project = metadata.get("project")
    if not isinstance(project, dict) or project.get("name") != "fieldkit-cli":
        return None
    return root
