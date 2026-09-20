"""Coverage tests for match_pursuits_for_account in fieldkit.ingest.router.

Targets the CRAP-flagged function ``match_pursuits_for_account``
(src/fieldkit/ingest/router.py:595, CRAP=50.38, complexity=10). Tests-only
change: no production code is touched. Assertions target observable behavior
(return values, call counts) rather than mere call-through.

Local helpers only — do not import from test_ingest_router.py per house
convention (new tests go in a new file per cluster).
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.ingest.router import match_pursuits_for_account

pytestmark = pytest.mark.unit


def _accounts_cfg(
    account_name: str,
    pursuit_dir: str | None = "pursuits",
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal accounts.yaml-shaped dict for a single account.

    Passing pursuit_dir=None omits the 'pursuit_dir' key entirely (distinct
    from passing '' which sets it to an empty string).
    """
    account_info: dict[str, Any] = {}
    if pursuit_dir is not None:
        account_info["pursuit_dir"] = pursuit_dir
    if keywords is not None:
        account_info["keywords"] = keywords
    return {"accounts": {account_name: account_info}}


def _write_pursuit_file(pursuit_dir: Path, name: str, h1: str = "") -> Path:
    """Write a pursuit markdown file with an optional H1 heading line."""
    pursuit_dir.mkdir(parents=True, exist_ok=True)
    path = pursuit_dir / name
    content = f"# {h1}\n\nBody text.\n" if h1 else "Body text.\n"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. pursuit_dir_raw empty -> [] (early return, baseline-covered; kept green)
# ---------------------------------------------------------------------------


def test_missing_pursuit_dir_key_returns_empty_list(tmp_path: Path) -> None:
    cfg = _accounts_cfg("acme-corp", pursuit_dir=None)
    with patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg):
        result = match_pursuits_for_account("acme-corp", keywords=["rhoai"], data_root=tmp_path)
    assert result == []


def test_blank_pursuit_dir_value_returns_empty_list(tmp_path: Path) -> None:
    cfg = _accounts_cfg("acme-corp", pursuit_dir="")
    with patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg):
        result = match_pursuits_for_account("acme-corp", keywords=["rhoai"], data_root=tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# 2/3. resolved_root ternary: data_root=None vs data_root=<Path>
# ---------------------------------------------------------------------------


def test_data_root_none_falls_back_to_get_fieldkit_home(tmp_path: Path) -> None:
    """data_root=None -> resolved_root comes from get_fieldkit_home(), called once,
    and its return value is the path actually used to build pursuit_dir (proven by
    a real match only reachable if resolved_root == tmp_path)."""
    account_name = "acme-corp"
    pursuit_dir = tmp_path / "pursuits"
    _write_pursuit_file(pursuit_dir, "acme-corp-rhoai-2026.md")
    cfg = _accounts_cfg(account_name, pursuit_dir="pursuits")
    with (
        patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg),
        patch("fieldkit.ingest.router.get_fieldkit_home", return_value=tmp_path) as mock_home,
        patch("fieldkit.ingest.router._read_pursuit_h1", return_value=""),
    ):
        result = match_pursuits_for_account(account_name, keywords=["rhoai rollout"], data_root=None)
    mock_home.assert_called_once()
    assert result == ["acme-corp-rhoai-2026"]


def test_data_root_provided_does_not_call_get_fieldkit_home(tmp_path: Path) -> None:
    """data_root=<Path> -> get_fieldkit_home() must not be invoked at all."""
    account_name = "acme-corp"
    pursuit_dir = tmp_path / "pursuits"
    _write_pursuit_file(pursuit_dir, "acme-corp-rhoai-2026.md")
    cfg = _accounts_cfg(account_name, pursuit_dir="pursuits")
    with (
        patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg),
        patch("fieldkit.ingest.router.get_fieldkit_home") as mock_home,
        patch("fieldkit.ingest.router._read_pursuit_h1", return_value=""),
    ):
        result = match_pursuits_for_account(account_name, keywords=["rhoai rollout"], data_root=tmp_path)
    mock_home.assert_not_called()
    assert result == ["acme-corp-rhoai-2026"]


# ---------------------------------------------------------------------------
# 4. pursuit_dir.is_dir() False -> [] and the glob loop never runs
# ---------------------------------------------------------------------------


def test_pursuit_dir_not_a_directory_skips_glob_and_returns_empty_list(tmp_path: Path) -> None:
    """A nonexistent pursuit_dir short-circuits before any glob() call.

    Path.glob() on a missing directory silently yields no matches in this
    pathlib version, so a bare return-value check can't distinguish 'guarded'
    from 'unguarded'. Patching Path.glob to assert it was never called makes
    the short-circuit itself the assertion, not just its (coincidental) result.
    """
    account_name = "acme-corp"
    cfg = _accounts_cfg(account_name, pursuit_dir="missing-pursuits")  # never created under tmp_path
    with (
        patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg),
        patch.object(Path, "glob") as mock_glob,
    ):
        result = match_pursuits_for_account(account_name, keywords=["rhoai"], data_root=tmp_path)
    assert result == []
    mock_glob.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Mixed pursuit dir: .template skip, gmail-intel skip, slug match, H1
#    match, non-match exclusion — exact result in sorted glob order.
# ---------------------------------------------------------------------------


def test_mixed_pursuit_files_slug_and_h1_matches_with_skips(tmp_path: Path) -> None:
    account_name = "acme-corp"
    pursuit_dir = tmp_path / "pursuits"
    pursuit_dir.mkdir()

    # a: matches via slug token 'rhoai' (caller keyword).
    (pursuit_dir / "a-acme-rhoai-2026.md").write_text("Body.\n", encoding="utf-8")
    # b: would match via slug token 'quarterly' (account keyword) if not
    #    skipped by the '.template' guard.
    (pursuit_dir / "b-acme-quarterly-review.template.md").write_text("Body.\n", encoding="utf-8")
    # c: would match via slug token 'rhoai' (caller keyword) if not skipped
    #    by the 'gmail-intel' guard.
    (pursuit_dir / "c-rhoai-gmail-intel-thread.md").write_text("Body.\n", encoding="utf-8")
    # d: no slug match; matches only via its H1 heading (account keyword 'quarterly').
    (pursuit_dir / "d-acme-project-x.md").write_text("Body.\n", encoding="utf-8")
    # e: matches neither slug nor H1 — excluded.
    (pursuit_dir / "e-acme-unrelated.md").write_text("Body.\n", encoding="utf-8")

    h1_by_filename = {
        "a-acme-rhoai-2026.md": "",
        "b-acme-quarterly-review.template.md": "",
        "c-rhoai-gmail-intel-thread.md": "",
        "d-acme-project-x.md": "Quarterly Business Review",
        "e-acme-unrelated.md": "General Notes",
    }

    def _fake_read_h1(path: Path) -> str:
        return h1_by_filename[path.name]

    cfg = _accounts_cfg(account_name, pursuit_dir="pursuits", keywords=["quarterly"])
    with (
        patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg),
        patch("fieldkit.ingest.router._read_pursuit_h1", side_effect=_fake_read_h1),
    ):
        result = match_pursuits_for_account(account_name, keywords=["RHOAI rollout"], data_root=tmp_path)

    assert result == ["a-acme-rhoai-2026", "d-acme-project-x"]


# ---------------------------------------------------------------------------
# 6. Keyword combination: normalized caller keywords + account keywords both
#    participate, independently on each side of the '+'.
# ---------------------------------------------------------------------------


def test_match_via_account_keyword_only(tmp_path: Path) -> None:
    """Caller passes no usable keywords; only the account's own keyword matches."""
    account_name = "acme-corp"
    pursuit_dir = tmp_path / "pursuits"
    _write_pursuit_file(pursuit_dir, "acme-corp-phoenix-initiative.md")

    cfg = _accounts_cfg(account_name, pursuit_dir="pursuits", keywords=["phoenix"])
    with patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg):
        result = match_pursuits_for_account(account_name, keywords=[], data_root=tmp_path)

    assert result == ["acme-corp-phoenix-initiative"]


def test_match_via_caller_keyword_only(tmp_path: Path) -> None:
    """Account has no configured keywords; only the caller's keyword matches."""
    account_name = "acme-corp"
    pursuit_dir = tmp_path / "pursuits"
    _write_pursuit_file(pursuit_dir, "acme-corp-phoenix-initiative.md")

    cfg = _accounts_cfg(account_name, pursuit_dir="pursuits", keywords=None)
    with patch("fieldkit.ingest.router._load_accounts_config", return_value=cfg):
        result = match_pursuits_for_account(account_name, keywords=["Phoenix rollout"], data_root=tmp_path)

    assert result == ["acme-corp-phoenix-initiative"]
