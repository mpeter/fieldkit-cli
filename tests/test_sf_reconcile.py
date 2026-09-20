"""Tests for sf_pipeline.reconcile — Key Fields table rewriting from frontmatter."""

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf import reconcile
from fieldkit.commands.sf.reconcile import cli

pytestmark = pytest.mark.unit

# ── _parse_frontmatter ──────────────────────────────────────────────────────


# ── TestParseFrontmatter (flattened) ────────────────────────────────────────


def test_parse_frontmatter_basic() -> None:
    text = "---\ntitle: Deal\nsf_stage: Negotiate\nsf_arr: 50000\n---\n# Body\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm["title"] == "Deal"
    assert fm["sf_stage"] == "Negotiate"
    assert fm["sf_arr"] == "50000"


def test_parse_frontmatter_no_frontmatter() -> None:
    assert reconcile._parse_frontmatter("# Just a heading") == {}


def test_parse_frontmatter_strips_quotes() -> None:
    text = "---\ntitle: \"Quoted\"\nother: 'single'\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm["title"] == "Quoted"
    assert fm["other"] == "single"


def test_parse_frontmatter_ignores_indented_lines() -> None:
    # yaml.safe_load parses nested mappings as dicts; the coercion filter
    # excludes them so only scalar keys survive.
    text = "---\nmeddpicc:\n  metrics: 2\ntitle: X\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert "metrics" not in fm
    assert "meddpicc" not in fm  # nested dict excluded
    assert fm["title"] == "X"


def test_parse_frontmatter_colon_in_value() -> None:
    # YAML-quoted colon-in-value parses correctly.
    text = '---\nnote: "key: value here"\n---\n'
    fm = reconcile._parse_frontmatter(text)
    assert fm["note"] == "key: value here"


def test_parse_frontmatter_ambiguous_colon_returns_empty() -> None:
    # Unquoted double-colon mapping is ambiguous YAML → YAMLError → {}
    text = "---\nnote: key: value here\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm == {}


def test_parse_frontmatter_yaml_type_coercion() -> None:
    text = "---\nactive: true\ncount: 42\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm["active"] == "True"
    assert fm["count"] == "42"


def test_parse_frontmatter_malformed_yaml_returns_empty() -> None:
    text = "---\n{{invalid\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm == {}


def test_parse_frontmatter_url_colon_no_space() -> None:
    # Colon with no trailing space is valid YAML (not a key separator).
    text = "---\nurl: https://example.com\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert fm["url"] == "https://example.com"


def test_parse_frontmatter_null_values_excluded() -> None:
    # YAML null (bare key with no value) is excluded from the result.
    text = "---\nempty:\ntitle: Present\n---\n"
    fm = reconcile._parse_frontmatter(text)
    assert "empty" not in fm
    assert fm["title"] == "Present"


# ── _rewrite_key_fields_table ───────────────────────────────────────────────

_TABLE_DOC = """\
---
sf_stage: Closed Won
sf_close_date: 2025-07-01
sf_arr: 75000
---

## Key Fields

| Field      | Value      |
| ---------- | ---------- |
| Stage      | Negotiate  |
| Close Date | 2025-06-01 |
| ACV        | 50000      |
| Owner      | Alice      |
"""


# ── TestRewriteKeyFieldsTable (flattened) ───────────────────────────────────


def test_rewrite_key_fields_table_updates_mapped_rows() -> None:
    fm = {"sf_stage": "Closed Won", "sf_close_date": "2025-07-01", "sf_arr": "75000"}
    new_text, changes = reconcile._rewrite_key_fields_table(_TABLE_DOC, fm)
    assert changes == 3
    assert "Closed Won" in new_text
    assert "2025-07-01" in new_text
    assert "75000" in new_text


def test_rewrite_key_fields_table_leaves_unmapped_rows() -> None:
    fm = {"sf_stage": "Closed Won", "sf_close_date": "2025-07-01", "sf_arr": "75000"}
    new_text, _ = reconcile._rewrite_key_fields_table(_TABLE_DOC, fm)
    assert "Alice" in new_text


def test_rewrite_key_fields_table_no_changes_when_values_match() -> None:
    fm = {"sf_stage": "Negotiate", "sf_close_date": "2025-06-01", "sf_arr": "50000"}
    _, changes = reconcile._rewrite_key_fields_table(_TABLE_DOC, fm)
    assert changes == 0


def test_rewrite_key_fields_table_no_table() -> None:
    text = "---\nsf_stage: X\n---\n# No table here\n"
    fm = {"sf_stage": "X"}
    new_text, changes = reconcile._rewrite_key_fields_table(text, fm)
    assert changes == 0
    assert new_text == text


def test_rewrite_key_fields_table_preserves_single_column_rows() -> None:
    text = "## Key Fields\n\n| Field | Value |\n| ----- |\n| dangling |\n"

    result = reconcile._rewrite_key_fields_table(text, {})

    assert result == (text, 0)


def test_rewrite_key_fields_table_partial_update() -> None:
    fm = {"sf_stage": "Closed Won"}
    new_text, changes = reconcile._rewrite_key_fields_table(_TABLE_DOC, fm)
    assert changes == 1
    assert "Closed Won" in new_text
    assert "2025-06-01" in new_text  # unchanged


# ── cli integration ─────────────────────────────────────────────────────────


# ── TestReconcileMain (flattened) ───────────────────────────────────────────


def test_run_reconcile_updates_file(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    p.write_text(_TABLE_DOC, encoding="utf-8")
    result = CliRunner().invoke(cli, [str(p)])
    assert result.exit_code == 0
    assert "Closed Won" in p.read_text()


def test_run_reconcile_skip_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "no_fm.md"
    p.write_text("# No frontmatter\n", encoding="utf-8")
    result = CliRunner().invoke(cli, [str(p)])
    assert result.exit_code == 0


def test_run_reconcile_skip_no_sf_keys(tmp_path: Path) -> None:
    p = tmp_path / "no_sf.md"
    p.write_text("---\ntitle: X\n---\n# Body\n", encoding="utf-8")
    result = CliRunner().invoke(cli, [str(p)])
    assert result.exit_code == 0


def test_run_reconcile_file_not_found() -> None:
    result = CliRunner().invoke(cli, ["/tmp/nonexistent_xyz.md"])
    assert result.exit_code != 0


# -- historic regression regression: _run_reconcile must return status not sys.exit -------


# ── TestBug141ReconcileNoSystemExit (flattened) ─────────────────────────────


def test_run_reconcile_run_reconcile_returns_updated(tmp_path: Path) -> None:
    """_run_reconcile returns updated when table rows changed (historic regression)."""
    p = tmp_path / "pursuit.md"
    p.write_text(_TABLE_DOC, encoding="utf-8")
    result = reconcile._run_reconcile(str(p))
    assert result == "updated", f"Expected updated, got {result!r}"


def test_run_reconcile_run_reconcile_returns_ok_no_changes(tmp_path: Path) -> None:
    """_run_reconcile returns ok when values already match (historic regression)."""
    doc = (
        "---\nsf_stage: Negotiate\nsf_close_date: 2025-06-01\nsf_arr: 50000\n---\n\n"
        "## Key Fields\n\n| Field      | Value      |\n| ---------- | ---------- |\n"
        "| Stage      | Negotiate  |\n| Close Date | 2025-06-01 |\n| ACV        | 50000      |\n"
    )
    p = tmp_path / "pursuit.md"
    p.write_text(doc, encoding="utf-8")
    result = reconcile._run_reconcile(str(p))
    assert result == "ok", f"Expected ok, got {result!r}"


def test_run_reconcile_run_reconcile_returns_skip_no_sf(tmp_path: Path) -> None:
    """_run_reconcile returns skip when no sf_ keys in frontmatter (historic regression)."""
    p = tmp_path / "no_sf.md"
    p.write_text("---\ntitle: X\n---\n# Body\n", encoding="utf-8")
    result = reconcile._run_reconcile(str(p))
    assert result == "skip", f"Expected skip, got {result!r}"


def test_run_reconcile_run_reconcile_does_not_raise_system_exit_on_success(tmp_path: Path) -> None:
    """_run_reconcile must NOT raise SystemExit on success (historic regression regression)."""
    p = tmp_path / "pursuit.md"
    p.write_text(_TABLE_DOC, encoding="utf-8")
    try:
        reconcile._run_reconcile(str(p))
    except SystemExit as exc:
        raise AssertionError(
            f"historic regression: _run_reconcile raised SystemExit({exc.code}) -- this would kill the listview account loop"
        ) from exc


def test_run_reconcile_run_reconcile_raises_file_not_found_not_system_exit(tmp_path: Path) -> None:
    """historic regression: _run_reconcile raises FileNotFoundError (not SystemExit) for missing files.

    Callers (e.g. do_write_opp in sync.py) can catch FileNotFoundError without
    the whole listview account loop being killed by a SystemExit.
    """
    missing = str(tmp_path / "does_not_exist.md")
    with pytest.raises(FileNotFoundError, match="Pursuit file not found"):
        reconcile._run_reconcile(missing)


def test_run_reconcile_run_reconcile_file_not_found_is_not_system_exit(tmp_path: Path) -> None:
    """historic regression: FileNotFoundError from _run_reconcile must NOT be a SystemExit."""
    missing = str(tmp_path / "ghost.md")
    try:
        reconcile._run_reconcile(missing)
    except FileNotFoundError:
        pass  # correct — FileNotFoundError is expected
    except SystemExit as exc:
        raise AssertionError(
            f"historic regression: _run_reconcile raised SystemExit({exc.code}) for missing file "
            "instead of FileNotFoundError — this kills the listview loop"
        ) from exc


# ── historic regression: swallowed-exception diagnostics ────────────────────────────────


def test_reconcile_dry_logs_warning_when_load_pursuit_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """historic regression: the fallback to raw frontmatter parsing must not be silent.

    Asserted at WARNING specifically. The CLI sets basicConfig(level=INFO) and reconcile
    has no --verbose flag, so a DEBUG record would be suppressed in every normal run —
    the fix would satisfy its work order while changing nothing an operator can see.
    """
    pursuit = tmp_path / "deal.md"
    pursuit.write_text("---\ntitle: Deal\nsf_stage: Negotiate\n---\n# Body\n", encoding="utf-8")

    def _boom(_path: object) -> object:
        raise ValueError("pydantic validation exploded")

    monkeypatch.setattr(reconcile, "load_pursuit", _boom)

    with caplog.at_level(logging.WARNING, logger="fieldkit.commands.sf.reconcile"):
        result = reconcile._run_reconcile_dry(str(pursuit))

    assert result is not None, "the raw-frontmatter fallback must still produce a result"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "load_pursuit failure was swallowed with no operator-visible record"
    assert "load_pursuit failed" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None, "the traceback is the diagnostic — exc_info must be attached"
