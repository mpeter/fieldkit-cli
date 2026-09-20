#!/usr/bin/env python3
"""
Pre-commit validator for pursuit file frontmatter.

Checks all staged accounts/*/pursuits/*.md files against:
  1. JSON Schema (config/pursuit-frontmatter.schema.json — root-level copy; this hook resolves via repo root)
  2. Hyphenated sf- key detection (schema additionalProperties: false catches this
     but we surface a friendlier message with the correct underscore equivalent)
  3. Presence of required fields: stage, gate-status, meddpicc

Usage:
  git pre-commit hook: called automatically on commit
  standalone:         python3 scripts/hooks/validate-pursuit-frontmatter.py [file ...]
"""

import datetime
import json
import re
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml


@cache
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@cache
def _schema_path() -> Path:
    return _repo_root() / "src" / "fieldkit" / "_data" / "pursuit-frontmatter.schema.json"


def get_staged_pursuit_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    files = []
    for line in result.stdout.splitlines():
        p = Path(line)
        parts = p.parts
        if len(parts) >= 4 and parts[0] == "accounts" and parts[2] == "pursuits" and p.suffix == ".md":
            files.append(_repo_root() / p)
    return files


def normalize_dates(obj: Any) -> Any:
    """Recursively convert PyYAML date/datetime objects to ISO strings.

    PyYAML auto-parses YYYY-MM-DD as datetime.date and ISO datetimes as
    datetime.datetime. The files store these as strings; we need string
    types to match the JSON Schema definition.
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: normalize_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_dates(v) for v in obj]
    return obj


def parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Extract and parse YAML frontmatter. Returns None if absent."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?\n)---\n", text, re.DOTALL)
    if not match:
        return None
    raw: dict[str, Any] | None = yaml.safe_load(match.group(1)) or {}
    return cast(dict[str, Any], normalize_dates(raw))


def check_hyphenated_sf_keys(fm: dict[str, Any]) -> list[str]:
    errors = []
    for key in fm:
        if re.match(r"^sf-[a-z]", key):
            fixed = key.replace("-", "_")
            errors.append(f"  hyphenated key '{key}' → use '{fixed}'")
    return errors


def validate_file(path: Path, schema: dict[str, Any]) -> list[str]:
    errors = []

    fm = parse_frontmatter(path)
    if fm is None:
        return ["  no YAML frontmatter found"]

    # Hyphenated sf- keys (friendly message before schema validation rejects them)
    hyph = check_hyphenated_sf_keys(fm)
    if hyph:
        errors.extend(hyph)

    # JSON Schema validation
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(fm), key=lambda e: list(e.path)):
        path_str = ".".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"  [{path_str}] {err.message}")

    return errors


def main(explicit_files: list[str] | None = None) -> int:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))

    files = [Path(f).resolve() for f in explicit_files] if explicit_files else get_staged_pursuit_files()

    if not files:
        return 0

    failed = 0
    for f in files:
        rel = f.relative_to(_repo_root())
        errors = validate_file(f, schema)
        if errors:
            print(f"✗ {rel}", file=sys.stderr)
            for e in errors:
                print(e, file=sys.stderr)
            failed += 1
        else:
            print(f"✓ {rel}")

    if failed:
        print(
            f"\n{failed} pursuit file(s) failed frontmatter validation. Commit blocked.",
            file=sys.stderr,
        )
        print(
            "Fix the errors above, then re-stage the files with `git add`.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    args = sys.argv[1:] if len(sys.argv) > 1 else None
    sys.exit(main(args))
