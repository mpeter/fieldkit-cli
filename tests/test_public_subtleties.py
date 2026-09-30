"""Repository-owned subtleties must remain portable and current."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUBTLETIES = _REPO_ROOT / "docs" / "subtleties"
_ROOT_OWNED = _SUBTLETIES / "config_subtleties.md"
_PRIVATE_MARKERS = (
    ".opencode",
    ".serena",
    "Exported from",
    "Serena",
    "mem:",
    "Red Hat",
)
_PRIVATE_TRACKER = re.compile(r"\b(?:BUG|ENH|OC)-\d+\b")


def _owned_pages() -> list[Path]:
    return [path for path in sorted(_SUBTLETIES.glob("*.md")) if path != _ROOT_OWNED]


@pytest.mark.parametrize("path", _owned_pages(), ids=lambda path: path.name)
def test_subtleties_have_no_private_provenance_or_tracker_context(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert not [marker for marker in _PRIVATE_MARKERS if marker in text]
    assert _PRIVATE_TRACKER.search(text) is None


def test_documentation_maintenance_routes_durable_knowledge_to_repository_files() -> None:
    text = (_SUBTLETIES / "memory_maintenance.md").read_text(encoding="utf-8")

    assert "AGENTS.md" in text
    assert "ROADMAP.md" in text
    assert "changelog.d/" in text
    assert "docs/subtleties/" in text
    assert "session" in text.lower()


def test_testing_subtlety_tracks_makefile_quality_limits() -> None:
    makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    text = (_SUBTLETIES / "testing.md").read_text(encoding="utf-8")

    crapload_limits = set(re.findall(r"--max-crapload (\d+)", makefile))
    contract_limits = set(re.findall(r"--min-contract-coverage (\d+)", makefile))

    assert len(crapload_limits) == 1
    assert len(contract_limits) == 1
    assert f"--max-crapload {crapload_limits.pop()}" in text
    assert f"--min-contract-coverage {contract_limits.pop()}" in text


def test_obsolete_private_tool_sync_page_is_removed() -> None:
    assert not (_SUBTLETIES / "uf-sync.md").exists()


def test_watcher_status_subtlety_names_the_canonical_entry_fields() -> None:
    text = (_SUBTLETIES / "watch_subtleties.md").read_text(encoding="utf-8")

    expected_fields = {
        "last_run",
        "outcome",
        "records_checked",
        "alerts_generated",
        "failures",
        "elapsed_seconds",
        "dry_run",
    }
    assert all(f"`{field}`" in text for field in expected_fields)
    assert "records_processed" not in text
    assert "records_failed" not in text


def test_provider_subtleties_do_not_encode_mock_or_timing_folklore() -> None:
    llm_text = (_SUBTLETIES / "llm_subtleties.md").read_text(encoding="utf-8")
    sf_text = (_SUBTLETIES / "sf_subtleties.md").read_text(encoding="utf-8")

    assert "~50ms" not in llm_text
    assert "silently targets a not-yet-imported module" not in llm_text
    assert "Never use `raise_for_status()`" not in sf_text
    assert "Retry only the classified transient statuses" in sf_text
