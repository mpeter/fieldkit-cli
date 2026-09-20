#!/usr/bin/env python3
"""
Pre-commit validator for account.md files.

Checks all staged accounts/*/account.md files for:
  1. Required sections (Account Overview, Stakeholder Map, etc.)
  2. Stakeholder Map has at least one table with Name, Title, SF Contact Role columns
  3. No garbage entries from Coverage Gaps taxonomy tables

Usage:
  git pre-commit hook: called automatically on commit
  standalone:         python3 scripts/hooks/validate-account-md.py [file ...]
"""

import re
import subprocess
import sys
from functools import cache
from pathlib import Path

from fieldkit.enrich.constants import GARBAGE_NAMES


@cache
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# Required sections in every account.md
REQUIRED_SECTIONS = [
    "Account Overview",
    "Why We're Here",
    "Stakeholder Map",
    "Competitive Landscape",
    "Expansion Opportunities",
]

# GARBAGE_NAMES imported from fieldkit.enrich.constants — single source of truth.


def get_staged_account_files() -> list[Path]:
    """Get all staged accounts/*/account.md files."""
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
        # Match accounts/[account-name]/account.md (not .template)
        if len(parts) == 3 and parts[0] == "accounts" and parts[1] != ".template" and parts[2] == "account.md":
            files.append(_repo_root() / p)
    return files


def validate_required_sections(path: Path, content: str) -> list[str]:
    """Check that all required sections are present."""
    errors = []

    for section in REQUIRED_SECTIONS:
        pattern = rf"##\s+{re.escape(section)}"
        if not re.search(pattern, content, re.IGNORECASE):
            errors.append(f"Missing required section '## {section}'")

    return errors


def validate_stakeholder_table(path: Path, content: str) -> list[str]:
    """Check that Stakeholder Map has a valid table with required columns."""
    errors = []

    # Find Stakeholder Map section
    stakeholder_match = re.search(
        r"##\s+Stakeholder(?:\s+Map)?.*?\n+(.*?)(?=\n## [^#]|\Z)", content, re.DOTALL | re.IGNORECASE
    )

    if not stakeholder_match:
        return ["Stakeholder Map section found but couldn't parse content"]

    section = stakeholder_match.group(1)

    # Find table headers (look for | Name | ... followed by separator row)
    table_headers = re.findall(r"(\|[^\n]+\|)\n\s*\|[\s-]+\|", section)

    if not table_headers:
        return ["Stakeholder Map section has no tables"]

    # Check at least one table has required columns
    found_valid_table = False

    for header in table_headers:
        cols = [c.strip().lower() for c in header.split("|") if c.strip()]

        # Skip Coverage Gaps table (has Role | Status | Action Needed)
        if "status" in cols and "action needed" in cols:
            continue

        # Check for required columns
        has_name = any("name" in col for col in cols)
        has_title = any("title" in col for col in cols)
        has_role = any("role" in col or "contact role" in col for col in cols)

        if has_name and has_title and has_role:
            found_valid_table = True
            break

    if not found_valid_table:
        errors.append("Stakeholder Map has no table with required columns (Name, Title, SF Contact Role)")

    return errors


def validate_no_garbage_in_tables(path: Path, content: str) -> list[str]:
    """Warn if table rows contain garbage names from Coverage Gaps section."""
    warnings = []

    # Find Stakeholder Map section
    stakeholder_match = re.search(
        r"##\s+Stakeholder(?:\s+Map)?.*?\n+(.*?)(?=\n## [^#]|\Z)", content, re.DOTALL | re.IGNORECASE
    )

    if not stakeholder_match:
        return []

    section = stakeholder_match.group(1)

    # Remove Coverage Gaps subsection
    section = re.sub(r"###\s+Coverage Gaps.*?(?=\n###|\n##|\Z)", "", section, flags=re.DOTALL | re.IGNORECASE)

    # Look for garbage names in remaining tables
    for garbage_name in GARBAGE_NAMES:
        # Check if name appears at start of a table row
        if re.search(rf"^\|\s*{re.escape(garbage_name)}\s*\|", section, re.MULTILINE):
            warnings.append(
                f"Warning: Found '{garbage_name}' in stakeholder table - may be a role type from Coverage Gaps section"
            )

    return warnings


def validate_account_file(path: Path) -> tuple[list[str], list[str]]:
    """Validate a single account.md file. Returns (errors, warnings)."""
    if not path.exists():
        return ([f"File not found: {path}"], [])

    content = path.read_text(encoding="utf-8")
    errors = []
    warnings = []

    # Check required sections
    errors.extend(validate_required_sections(path, content))

    # Check stakeholder table structure
    errors.extend(validate_stakeholder_table(path, content))

    # Check for garbage entries
    warnings.extend(validate_no_garbage_in_tables(path, content))

    return (errors, warnings)


def main() -> None:
    """Main validation entry point."""
    # Get files to validate
    # Validate specific files if passed as args (PostToolUse / testing),
    # else use staged files (pre-commit mode).
    # When called with explicit paths, filter to account.md files only so that
    # wiring as a PostToolUse hook on all Edit/Write events doesn't produce
    # false failures on non-account files.
    if len(sys.argv) > 1:
        files = [Path(p) for p in sys.argv[1:] if Path(p).name == "account.md"]
    else:
        files = get_staged_account_files()

    if not files:
        sys.exit(0)  # No account.md files to validate

    all_errors = []
    all_warnings = []

    for path in files:
        account_name = path.parent.name
        errors, warnings = validate_account_file(path)

        if errors:
            print(f"\n❌ {account_name}/account.md validation FAILED:", file=sys.stderr)
            for error in errors:
                print(f"   - {error}", file=sys.stderr)
            all_errors.extend(errors)

        if warnings:
            print(f"\n⚠️  {account_name}/account.md warnings:", file=sys.stderr)
            for warning in warnings:
                print(f"   - {warning}", file=sys.stderr)
            all_warnings.extend(warnings)

    if all_errors:
        print(f"\n❌ Account.md validation failed with {len(all_errors)} error(s).", file=sys.stderr)
        print("See config/account-md.schema.md for structure requirements.", file=sys.stderr)
        sys.exit(1)

    if all_warnings:
        print(f"\n⚠️  Account.md validation passed with {len(all_warnings)} warning(s).", file=sys.stderr)

    # Success
    if files:
        accounts = ", ".join(f.parent.name for f in files)
        print(f"✅ Account.md validation passed: {accounts}")

    sys.exit(0)


if __name__ == "__main__":
    main()
