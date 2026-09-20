"""Verify implementation change keeps gws as its sole Google Tasks integration."""

import ast
import hashlib
import sys
from pathlib import Path

_EXPECTED_MANIFESTS = {
    "pyproject.toml": "4aeb0070c3da0e745f7b6db1a33f2c06c5cf726fe28c49bdd61bd2a4122c8d15",
    "uv.lock": "94dfbf1a1391539c6474701a85a463d6e3b7fd31afac11e1b474efa0db035a89",
}
_PYTHON_SURFACE = (
    "src/fieldkit/companion/outbox.py",
    "src/fieldkit/web/actions.py",
    "src/fieldkit/web/data.py",
    "src/fieldkit/web/server.py",
)
_FORBIDDEN_IMPORT_ROOTS = frozenset({"google", "googleapiclient", "mcp"})


def check(root: Path, *, expected_manifests: dict[str, str] = _EXPECTED_MANIFESTS) -> list[str]:
    """Return non-scope violations under *root*."""
    failures: list[str] = []
    for name, expected in expected_manifests.items():
        path = root / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"{name} changed; implementation change adds no dependency surface")

    if (root / "fieldkit-home" / "gas").exists():
        failures.append("fieldkit-home/gas exists in the candidate; GAS is outside implementation change")

    for relative in _PYTHON_SURFACE:
        path = root / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                line = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
                line = node.lineno
            else:
                continue
            for module in modules:
                root_name = module.split(".", 1)[0].lower()
                if root_name in _FORBIDDEN_IMPORT_ROOTS or "oauth" in root_name:
                    failures.append(f"{relative}:{line} imports forbidden integration {module}")
    return failures


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    failures = check(root)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("implementation change non-scope: manifests unchanged; no GAS or Google client/OAuth/MCP imports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
