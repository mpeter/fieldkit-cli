#!/usr/bin/env python3
"""Advisory frontmatter validation for pursuit files.

Called as a post-write hook: validate_pursuit.py <file-path>
Always exits 0 — validation failures are advisory, never blocking.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve the fieldkit binary once at module load.
# file_arg is a repo-relative path provided by the pre-commit framework via
# sys.argv — git guarantees it is within the working tree. list-form
# subprocess.run() prevents shell injection regardless of path content.
_fieldkit_bin = shutil.which("fieldkit")
_cmd_base = (
    [_fieldkit_bin, "sf", "frontmatter"] if _fieldkit_bin else [sys.executable, "-m", "fieldkit", "sf", "frontmatter"]
)
if _fieldkit_bin is None:
    print(
        f"[validate_pursuit] WARNING: 'fieldkit' binary not found in PATH — "
        f"falling back to '{sys.executable} -m fieldkit'. Run 'make install' to fix.",
        file=sys.stderr,
    )


def main() -> int:
    file_arg = sys.argv[1] if len(sys.argv) > 1 else ""

    # Silently skip if no file given or not a pursuit markdown file.
    if not file_arg:
        return 0

    if not re.search(r"accounts/.+/pursuits/.+\.md$", file_arg):
        return 0

    # Resolve the repo root relative to this script so it works from any CWD.
    script_dir = Path(__file__).parent.resolve()
    repo_root = script_dir.parent

    # Run validation; capture stderr; suppress non-zero exit.
    try:
        result = subprocess.run(
            [*_cmd_base, "--validate", "--file", file_arg],
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
            timeout=30,
        )
        errors = result.stderr.strip()
    except Exception:  # noqa: BLE001 — advisory hook, swallow subprocess errors
        errors = ""

    if errors:
        # Filter out schema-not-found warnings — only surface real errors.
        real_errors = "\n".join(line for line in errors.splitlines() if not line.startswith("WARNING:")).strip()
        if real_errors:
            print(f"⚠ FRONTMATTER: {file_arg}", file=sys.stderr)
            for line in real_errors.splitlines():
                print(f"  {line}", file=sys.stderr)

    # Run MEDDPICC quality checks and Backstory prohibition scan (advisory only).
    try:
        result = subprocess.run(
            [*_cmd_base, "--quality-check", "--file", file_arg],
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
            timeout=30,
        )
        quality = result.stdout.strip() + result.stderr.strip()
        quality = quality.strip()
    except Exception:  # noqa: BLE001 — advisory hook, swallow subprocess errors
        quality = ""

    if quality:
        print(f"⚠ QUALITY: {file_arg}", file=sys.stderr)
        for line in quality.splitlines():
            print(f"  {line}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
