"""Missing-only workspace scaffold writes for initialization."""

import json
import os
import tempfile
from pathlib import Path

import click


def write_judgment_configs(data_dir: Path) -> None:
    """Create missing empty judgment files without replacing operator content."""
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schemas: dict[str, dict[str, list[object]]] = {
        "clocks.json": {"clocks": []},
        "engines.json": {"engines": []},
        "people.json": {"people": []},
        "watchlist.json": {"opportunities": []},
    }
    for filename, schema in schemas.items():
        path = config_dir / filename
        if path.exists() or path.is_symlink():
            continue
        fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=f".{filename}.", suffix=".tmp", text=True)
        tmp_path = Path(tmp_name)
        created = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(schema, tmp_file, indent=2)
                tmp_file.write("\n")
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            try:
                os.link(tmp_path, path)
                created = True
            except FileExistsError:
                pass
        finally:
            tmp_path.unlink(missing_ok=True)
        if created:
            click.echo(f"    ✓ {path}")
