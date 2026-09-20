#!/usr/bin/env python3
"""Check that the committed pursuit-frontmatter.schema.json covers all fields in the live Pydantic model.

Detects the primary drift scenario: a new ``sf_*`` field added to ``PursuitFrontmatter``
that was not reflected in the committed schema file.

Usage:
    uv run python scripts/check_schema_sync.py

Exit 0: schema covers all model fields.
Exit 1: schema is missing fields — run `make update-schema` to regenerate.

Note: The committed schema is hand-crafted and intentionally extends the Pydantic-generated
schema with additional constraints (e.g. allowing empty strings for monetary fields that the
SF sync code writes as ``""`` for null values). This script checks field coverage, not
byte-for-byte equality.
"""

import json
import sys
from pathlib import Path

# Resolve repo root (this script lives in scripts/)
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "src" / "fieldkit" / "_data" / "pursuit-frontmatter.schema.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

from fieldkit.pursuit.models import PursuitFrontmatter  # noqa: E402


def main() -> int:
    """Check that all Pydantic model fields appear in the committed schema.

    Returns:
        0 when the committed schema covers all model fields.
        1 when the committed schema is missing one or more model fields.
    """
    live_schema = PursuitFrontmatter.model_json_schema()
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    # Collect field names from the live Pydantic schema.
    # model_json_schema() puts top-level fields in 'properties'.
    live_props: set[str] = set(live_schema.get("properties", {}).keys())

    # Collect field names from the committed schema.
    committed_props: set[str] = set(committed.get("properties", {}).keys())

    missing = live_props - committed_props
    if not missing:
        print("Schema is up to date.")
        return 0

    print("ERROR: pursuit-frontmatter.schema.json is missing fields from the live Pydantic model.", file=sys.stderr)
    print("Run `make update-schema` to regenerate.", file=sys.stderr)
    for field in sorted(missing):
        print(f"  Missing field: {field!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
