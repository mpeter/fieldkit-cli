"""Branch coverage tests for fieldkit.watch.morning_brief_render._render_project_health_section.

Tests all branches: no projects, only healthy, zombie/expiring/soon combinations,
import error (exception swallowed), and classify_project returning None.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_render import (
    _collect_degraded_sources,
    _render_cross_account_section,
    _render_degraded_section,
    _render_project_health_section,
    _render_stall_blocks_grouped,
    _strip_alert_headings,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _render_project_health_section — all branches
# ---------------------------------------------------------------------------


# ── TestRenderProjectHealthSection (flattened) ──────────────────────────────


def _render_project_health_section_make_row(health: str) -> SimpleNamespace:
    return SimpleNamespace(health=health)


def test_render_project_health_section_no_projects_returns_empty(tmp_path: Path) -> None:
    """No project files → returns []."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.pursuit.projects.classify_project", return_value=None),
    ):
        result = _render_project_health_section()

    assert result == []


def test_render_project_health_section_all_healthy_rows_returns_empty(tmp_path: Path) -> None:
    """All rows are HEALTHY → returns []."""
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    proj = accounts_dir / "deal.md"
    proj.write_text("# Project", encoding="utf-8")

    rows = [_render_project_health_section_make_row("HEALTHY")]

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.pursuit.projects.classify_project", side_effect=lambda *a, **kw: rows.pop(0)),
    ):
        result = _render_project_health_section()

    assert result == []


def test_render_project_health_section_zombie_row_included(tmp_path: Path) -> None:
    """ZOMBIE projects appear in the section."""
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.pursuit.projects.classify_project",
            return_value=_render_project_health_section_make_row("ZOMBIE"),
        ),
    ):
        result = _render_project_health_section()

    assert any("ZOMBIE" in line for line in result)
    assert result[0] == "## Project Health"


def test_render_project_health_section_expiring_row_included(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.pursuit.projects.classify_project",
            return_value=_render_project_health_section_make_row("EXPIRING"),
        ),
    ):
        result = _render_project_health_section()

    assert any("EXPIRING" in line for line in result)


def test_render_project_health_section_soon_row_included(tmp_path: Path) -> None:
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.pursuit.projects.classify_project",
            return_value=_render_project_health_section_make_row("SOON"),
        ),
    ):
        result = _render_project_health_section()

    assert any("SOON" in line for line in result)


def test_render_project_health_section_mixed_zombie_expiring_soon(tmp_path: Path) -> None:
    """Multiple unhealthy types produce all counts in the summary line."""
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    for i in range(3):
        (accounts_dir / f"deal{i}.md").write_text("# Project", encoding="utf-8")

    rows = [
        _render_project_health_section_make_row("ZOMBIE"),
        _render_project_health_section_make_row("EXPIRING"),
        _render_project_health_section_make_row("SOON"),
    ]

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.pursuit.projects.classify_project", side_effect=rows),
    ):
        result = _render_project_health_section()

    summary = " ".join(result)
    assert "ZOMBIE" in summary
    assert "EXPIRING" in summary
    assert "SOON" in summary


def test_render_project_health_section_classify_returns_none_skips_row(tmp_path: Path) -> None:
    """classify_project returning None must be excluded from rows."""
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.pursuit.projects.classify_project", return_value=None),
    ):
        result = _render_project_health_section()

    assert result == []


def test_render_project_health_section_exception_returns_empty() -> None:
    """Any exception during classify returns [] — never propagates."""
    with (
        patch(
            "fieldkit.watch.morning_brief_render.get_fieldkit_home",
            side_effect=RuntimeError("no data root"),
        ),
    ):
        result = _render_project_health_section()

    assert result == []


def test_render_project_health_section_import_error_returns_empty() -> None:
    """ImportError (missing projects_health module) returns []."""
    # Patch the correct import path used by the render module and isolate
    # get_fieldkit_home so the real config (data_repo alias) doesn't resolve.
    with (
        patch.dict("sys.modules", {"fieldkit.pursuit.projects": None}),
        patch(
            "fieldkit.watch.morning_brief_render.get_fieldkit_home",
            side_effect=ImportError("projects_health not available"),
        ),
    ):
        result = _render_project_health_section()

    assert result == []


def test_render_project_health_section_output_includes_fieldkit_command_hint(tmp_path: Path) -> None:
    """Output includes the run-command hint."""
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.pursuit.projects.classify_project",
            return_value=_render_project_health_section_make_row("ZOMBIE"),
        ),
    ):
        result = _render_project_health_section()

    assert any("fieldkit pursuit projects" in line for line in result)


def test_render_project_health_section_does_not_include_trailing_separator(tmp_path: Path) -> None:
    """historic regression: section does NOT include a trailing '---' separator.

    The footer in render_brief() provides the separator — including one
    here caused a double '---' at the end of the rendered brief.
    """
    accounts_dir = tmp_path / "accounts" / "acme" / "projects"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "deal.md").write_text("# Project", encoding="utf-8")

    with (
        patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path),
        patch(
            "fieldkit.pursuit.projects.classify_project",
            return_value=_render_project_health_section_make_row("ZOMBIE"),
        ),
    ):
        result = _render_project_health_section()

    assert "---" not in result
    assert "## Project Health" in result


# ---------------------------------------------------------------------------
# _collect_degraded_sources — branch coverage
# ---------------------------------------------------------------------------


# ── TestCollectDegradedSources (flattened) ──────────────────────────────────


def test_collect_degraded_sources_healthy_sources_excluded() -> None:
    sources = {"Calendar": [{"title": "Meeting"}], "Gmail": []}
    result = _collect_degraded_sources(sources)
    assert result == []


def test_collect_degraded_sources_string_result_included() -> None:
    sources = {"Backstory": "unavailable: timeout"}
    result = _collect_degraded_sources(sources)
    assert len(result) == 1
    label, reason = result[0]
    assert label == "Backstory"
    assert "timeout" in reason


def test_collect_degraded_sources_unavailable_prefix_stripped() -> None:
    sources = {"SF": "unavailable: API quota exceeded"}
    result = _collect_degraded_sources(sources)
    _, reason = result[0]
    assert reason == "API quota exceeded"
    assert "unavailable:" not in reason


def test_collect_degraded_sources_reason_truncated_to_100_chars() -> None:
    long_reason = "x" * 200
    sources = {"Long": long_reason}
    result = _collect_degraded_sources(sources)
    _, reason = result[0]
    assert len(reason) <= 100


def test_collect_degraded_sources_empty_reason_becomes_unavailable() -> None:
    sources = {"Empty": ""}
    result = _collect_degraded_sources(sources)
    _, reason = result[0]
    assert reason == "unavailable"


def test_collect_degraded_sources_mixed_healthy_and_degraded() -> None:
    sources: dict[str, list | str] = {
        "OK": [{"title": "item"}],
        "Broken": "error occurred",
    }
    result = _collect_degraded_sources(sources)
    assert len(result) == 1
    assert result[0][0] == "Broken"


# ---------------------------------------------------------------------------
# _render_degraded_section — branch coverage
# ---------------------------------------------------------------------------


# ── TestRenderDegradedSection (flattened) ───────────────────────────────────


def test_render_degraded_section_empty_degraded_returns_empty() -> None:
    assert _render_degraded_section([]) == []


def test_render_degraded_section_single_degraded_source() -> None:
    result = _render_degraded_section([("Calendar", "timeout")])
    assert "## Degraded Sources" in result
    assert any("Calendar" in line and "timeout" in line for line in result)


def test_render_degraded_section_multiple_degraded_sources() -> None:
    result = _render_degraded_section([("A", "err1"), ("B", "err2")])
    assert sum(1 for line in result if line.startswith("- ")) == 2


# ---------------------------------------------------------------------------
# _render_stall_blocks_grouped — branch coverage
# ---------------------------------------------------------------------------


# ── TestRenderStallBlocksGrouped (flattened) ────────────────────────────────


def test_render_stall_blocks_grouped_error_string_passed_through() -> None:
    result = _render_stall_blocks_grouped("_Source unavailable_")
    assert result == ["_Source unavailable_"]


def test_render_stall_blocks_grouped_empty_list_returns_no_stalls() -> None:
    result = _render_stall_blocks_grouped([])
    assert "_No pursuit stall alerts today._" in result


def test_render_stall_blocks_grouped_groups_blocks_by_account() -> None:
    blocks = [
        "**Account:** `acme-corp`\nDeal stalled for 30 days",
        "**Account:** `acme-corp`\nAnother stall",
        "**Account:** `globalpay`\nGlobal stall",
    ]
    result = _render_stall_blocks_grouped(blocks)
    text = "\n".join(result)
    assert "acme-corp" in text
    assert "globalpay" in text
    assert "2 stall" in text


def test_render_stall_blocks_grouped_block_without_account_grouped_under_other() -> None:
    blocks = ["No account header here"]
    result = _render_stall_blocks_grouped(blocks)
    text = "\n".join(result)
    assert "other" in text


# ---------------------------------------------------------------------------
# _render_cross_account_section — branch coverage
# ---------------------------------------------------------------------------


# ── TestRenderCrossAccountSection (flattened) ───────────────────────────────


def test_render_cross_account_section_empty_returns_empty() -> None:
    assert _render_cross_account_section([]) == []


def test_render_cross_account_section_single_signal() -> None:
    signals = [{"topic": "Container adoption", "accounts": ["acme-corp", "globalpay"]}]
    result = _render_cross_account_section(signals)
    assert "## Cross-Account Signals" in result
    assert any("Container adoption" in line for line in result)
    assert any("acme-corp" in line for line in result)


def test_render_cross_account_section_multiple_signals() -> None:
    signals = [
        {"topic": "OpenShift", "accounts": ["a"]},
        {"topic": "RHEL", "accounts": ["b", "c"]},
    ]
    result = _render_cross_account_section(signals)
    assert sum(1 for line in result if line.startswith("- ")) == 2


# ---------------------------------------------------------------------------
# _strip_alert_headings (historic regression)
# ---------------------------------------------------------------------------


# ── TestStripAlertHeadings (flattened) ──────────────────────────────────────


def test_strip_alert_headings_removes_date_heading_and_outbox_heading() -> None:
    """Spec task 1.3: date + Pending Outbox headings stripped, content preserved."""
    block = "## 2026-06-15\n\n### Pending Outbox\n\n*0 draft(s) older than 24h...*"
    result = _strip_alert_headings(block)
    assert "## 2026-06-15" not in result
    assert "### Pending Outbox" not in result
    assert "*0 draft(s) older than 24h...*" in result


def test_strip_alert_headings_preserves_non_heading_content() -> None:
    """Content lines with no headings pass through unchanged."""
    block = "- Draft: subject line\n- Draft: another subject"
    result = _strip_alert_headings(block)
    assert "Draft: subject line" in result
    assert "Draft: another subject" in result


def test_strip_alert_headings_empty_block_returns_empty_string() -> None:
    """Empty input → empty output (no crash)."""
    assert _strip_alert_headings("") == ""


def test_strip_alert_headings_heading_only_block_returns_empty_string() -> None:
    """A block that is only headings produces an empty string."""
    block = "## 2026-06-15\n\n### Pending Outbox"
    result = _strip_alert_headings(block)
    assert result == ""


def test_strip_alert_headings_does_not_strip_h3_headings_other_than_pending_outbox() -> None:
    """### headings that are not 'Pending Outbox' are preserved."""
    block = "### Stall Alert\n\n*pursuit-slug: 5 days stalled*"
    result = _strip_alert_headings(block)
    assert "### Stall Alert" in result
    assert "*pursuit-slug: 5 days stalled*" in result


def test_strip_alert_headings_does_not_strip_h2_headings_that_are_not_dates() -> None:
    """## headings that are not YYYY-MM-DD dates are preserved."""
    block = "## Backstory Health\n\nscore 42 below threshold 50"
    result = _strip_alert_headings(block)
    assert "## Backstory Health" in result


# ---------------------------------------------------------------------------
# _render_champion_section with None (historic regression)
# ---------------------------------------------------------------------------


def test_render_champion_section_none_shows_unavail_message() -> None:
    """historic regression: _render_champion_section(None) shows gmail unavailable message."""
    from fieldkit.commands.pipeline.render import _render_champion_section

    result = _render_champion_section(None)
    assert "unavailable" in result.lower()
    assert "gmail" in result.lower()
