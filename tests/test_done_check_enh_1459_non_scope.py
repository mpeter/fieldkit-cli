"""Tests for the implementation change non-scope done checker."""

import hashlib
from pathlib import Path

import pytest

from scripts.done_checks.check_enh_1459_non_scope import check

pytestmark = pytest.mark.unit


def _root(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    manifests: dict[str, str] = {}
    for name in ("pyproject.toml", "uv.lock"):
        content = f"baseline {name}\n".encode()
        (tmp_path / name).write_bytes(content)
        manifests[name] = hashlib.sha256(content).hexdigest()
    surface = tmp_path / "src/fieldkit/web"
    surface.mkdir(parents=True)
    (surface / "data.py").write_text("import json\n", encoding="utf-8")
    return tmp_path, manifests


def test_accepts_unchanged_manifests_and_standard_library_imports(tmp_path: Path) -> None:
    root, manifests = _root(tmp_path)
    result = check(root, expected_manifests=manifests)
    assert result == []


def test_rejects_manifest_change(tmp_path: Path) -> None:
    root, manifests = _root(tmp_path)
    (root / "uv.lock").write_text("changed\n", encoding="utf-8")
    result = check(root, expected_manifests=manifests)
    assert result == ["uv.lock changed; implementation change adds no dependency surface"]


@pytest.mark.parametrize(
    "statement", ["import google", "from googleapiclient import discovery", "import mcp", "import oauthlib"]
)
def test_rejects_new_google_oauth_or_mcp_import(tmp_path: Path, statement: str) -> None:
    root, manifests = _root(tmp_path)
    (root / "src/fieldkit/web/data.py").write_text(statement + "\n", encoding="utf-8")
    result = check(root, expected_manifests=manifests)
    assert len(result) == 1
    assert "imports forbidden integration" in result[0]


def test_rejects_gas_surface(tmp_path: Path) -> None:
    root, manifests = _root(tmp_path)
    (root / "fieldkit-home/gas").mkdir(parents=True)
    result = check(root, expected_manifests=manifests)
    assert result == ["fieldkit-home/gas exists in the candidate; GAS is outside implementation change"]
