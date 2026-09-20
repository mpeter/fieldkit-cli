"""Tests for fieldkit.skill.template — _load_account_fields.

Covers the exception-fallback guards for get_account_names()/get_internal_domains(),
the real-indices population and gap-filling loop with a multi-account fixture, and
the primary_pursuit resolution block (directory check, glob, exception handling)
not covered by test_skill_template.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.skill.template import _MAX_ACCOUNT_INDEX, _load_account_fields

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_names(slugs: list[str] | None = None, side_effect: Exception | None = None) -> "patch":  # type: ignore[name-defined]
    if side_effect is not None:
        return patch("fieldkit.config.get_account_names", side_effect=side_effect)
    return patch("fieldkit.config.get_account_names", return_value=list(slugs or []))


def _patch_domains(domains: list[str] | None = None, side_effect: Exception | None = None) -> "patch":  # type: ignore[name-defined]
    if side_effect is not None:
        return patch("fieldkit.config.get_internal_domains", side_effect=side_effect)
    return patch("fieldkit.config.get_internal_domains", return_value=list(domains or []))


def _patch_root(root: Path) -> "patch":  # type: ignore[name-defined]
    return patch("fieldkit.config.get_accounts_root", return_value=root)


def _make_pursuits_dir(accounts_root: Path, slug: str, filenames: list[str]) -> Path:
    """Create <accounts_root>/<slug>/pursuits/ populated with the given filenames."""
    pursuits_dir = accounts_root / slug / "pursuits"
    pursuits_dir.mkdir(parents=True)
    for name in filenames:
        (pursuits_dir / name).write_text("content", encoding="utf-8")
    return pursuits_dir


# ---------------------------------------------------------------------------
# Exception-fallback guards
# ---------------------------------------------------------------------------


def test_get_account_names_exception_falls_back_to_empty_slugs() -> None:
    with _patch_names(side_effect=RuntimeError("boom")), _patch_domains(domains=["example.com"]):
        ctx = _load_account_fields()

    assert ctx["primary_account"] == ""
    assert ctx["accounts.all"] == ""


def test_get_internal_domains_exception_falls_back_to_empty_domains() -> None:
    """Separate guard from get_account_names — pair a SUCCEEDING slugs call so the
    guard under test is unambiguous."""
    with _patch_names(slugs=["acme"]), _patch_domains(side_effect=RuntimeError("boom")):
        ctx = _load_account_fields()

    assert ctx["internal_domain"] == ""
    assert ctx["primary_account"] == "acme"  # proves get_account_names succeeded


# ---------------------------------------------------------------------------
# Real-indices population and gap-filling
# ---------------------------------------------------------------------------


def test_multi_account_populates_real_indices_and_gap_fills_with_primary() -> None:
    with _patch_names(slugs=["acme", "globex"]), _patch_domains(domains=[]):
        ctx = _load_account_fields()

    assert ctx["accounts.0"] == "acme"
    assert ctx["accounts.1"] == "globex"
    assert ctx["primary_account"] == "acme"
    assert ctx["accounts.all"] == "acme, globex"
    for i in range(2, _MAX_ACCOUNT_INDEX):
        assert ctx[f"accounts.{i}"] == "acme"


def test_empty_slugs_list_fills_all_gap_indices_with_empty_string() -> None:
    """Legitimately empty accounts.yaml (not via exception) — every accounts.N
    index up to _MAX_ACCOUNT_INDEX is filled with the empty primary."""
    with _patch_names(slugs=[]), _patch_domains(domains=[]):
        ctx = _load_account_fields()

    assert ctx["primary_account"] == ""
    for i in range(_MAX_ACCOUNT_INDEX):
        assert ctx[f"accounts.{i}"] == ""


# ---------------------------------------------------------------------------
# primary_pursuit resolution
# ---------------------------------------------------------------------------


def test_primary_pursuit_resolves_to_alphabetically_first_stem(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    _make_pursuits_dir(accounts_root, "acme", ["zzz-later.md", "aaa-first.md"])

    with (
        _patch_names(slugs=["acme", "globex"]),
        _patch_domains(domains=[]),
        _patch_root(accounts_root),
    ):
        ctx = _load_account_fields()

    assert ctx["primary_pursuit"] == "aaa-first"


def test_primary_pursuit_empty_when_pursuits_dir_missing(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    (accounts_root / "acme").mkdir(parents=True)  # no pursuits/ subdir

    with _patch_names(slugs=["acme"]), _patch_domains(domains=[]), _patch_root(accounts_root):
        ctx = _load_account_fields()

    assert ctx["primary_pursuit"] == ""


def test_primary_pursuit_empty_when_pursuits_dir_has_no_md_files(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    pursuits_dir = accounts_root / "acme" / "pursuits"
    pursuits_dir.mkdir(parents=True)

    with _patch_names(slugs=["acme"]), _patch_domains(domains=[]), _patch_root(accounts_root):
        ctx = _load_account_fields()

    assert ctx["primary_pursuit"] == ""


def test_primary_pursuit_exception_leaves_rest_of_ctx_populated() -> None:
    """The outer except Exception around the pursuit-resolution block is scoped
    to that block only — the rest of ctx must already be populated."""
    with (
        _patch_names(slugs=["acme"]),
        _patch_domains(domains=[]),
        patch("fieldkit.config.get_accounts_root", side_effect=RuntimeError("boom")),
    ):
        ctx = _load_account_fields()

    assert ctx["primary_pursuit"] == ""
    assert ctx["example_sf_id"] == "006Pe000000ExampleId"
    assert ctx["primary_account"] == "acme"


def test_primary_pursuit_skipped_entirely_when_primary_falsy() -> None:
    """if primary: guard means get_accounts_root() is never even called."""
    with (
        _patch_names(slugs=[]),
        _patch_domains(domains=[]),
        patch("fieldkit.config.get_accounts_root") as mock_root,
    ):
        mock_root.return_value = MagicMock()
        ctx = _load_account_fields()

    mock_root.assert_not_called()
    assert ctx["primary_pursuit"] == ""
