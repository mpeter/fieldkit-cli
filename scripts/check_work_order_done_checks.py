#!/usr/bin/env python3
"""Validate structured done checks on every live work order without executing them."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from fieldkit.driver.done_checks import CheckerCheck, DoneCheckError, parse_done_checks

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))

from check_done_checkers import validate_checker  # noqa: E402

REPO_ROOT = SCRIPT_ROOT.parent
WORK_ORDER_ROOT = REPO_ROOT / "docs" / "work-orders"

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---(?:[ \t]*\r?\n|\Z)", re.DOTALL)


def _frontmatter(text: str) -> tuple[dict[str, object] | None, str | None]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, None
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "frontmatter must be an object"
    return value, None


def _declares_issues(text: str) -> bool:
    match = _FRONTMATTER_RE.match(text)
    return match is not None and re.search(r"(?m)^issues\s*:", match.group(1)) is not None


def live_work_orders(root: Path = WORK_ORDER_ROOT) -> tuple[Path, ...]:
    """Return Markdown files whose frontmatter declares an issues list."""
    result: list[Path] = []
    for path in sorted(root.glob("*.md")):
        frontmatter, _ = _frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter is not None and isinstance(frontmatter.get("issues"), list):
            result.append(path)
    return tuple(result)


def validate_work_orders(root: Path = WORK_ORDER_ROOT, repo_root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Return all contract/reference errors for live work orders."""
    errors: list[str] = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        frontmatter, frontmatter_error = _frontmatter(text)
        if frontmatter_error is not None and _declares_issues(text):
            errors.append(f"{path}: invalid YAML frontmatter: {frontmatter_error}")
            continue
        if frontmatter is None or "issues" not in frontmatter:
            continue
        issues = frontmatter["issues"]
        if not isinstance(issues, list) or not issues or any(not isinstance(issue, str) for issue in issues):
            errors.append(f"{path}: issues must be a nonempty list of strings")
            continue
        try:
            contract = parse_done_checks(text)
        except DoneCheckError as exc:
            errors.append(f"{path}: {exc}")
            continue
        for check in contract.checks:
            if not isinstance(check, CheckerCheck):
                continue
            checker_path = repo_root / check.checker
            if not checker_path.is_file():
                errors.append(f"{path}: checker does not exist: {check.checker}")
                continue
            errors.extend(f"{path}: {violation}" for violation in validate_checker(checker_path))
        if "```bash" in text:
            print(f"NOTE: {path}: legacy Bash fence is prose only")
    return tuple(errors)


def main() -> int:
    errors = validate_work_orders()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"work-order done-check policy: {len(live_work_orders())} live work order(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
