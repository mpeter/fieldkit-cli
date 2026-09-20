"""Tests for cross-account signal detection and rendering in morning_brief."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_collect import detect_cross_account_signals
from fieldkit.watch.morning_brief_render import _render_cross_account_section, render_brief

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_accounts(tmp_path: Path, accounts: dict[str, str | None]) -> Path:
    """Create a data-root structure with accounts/<slug>/gmail-intel.md files.

    accounts is a mapping of slug → gmail-intel.md content (None = file absent).
    Returns the data_root Path.
    """
    data_root = tmp_path / "data"
    for slug, content in accounts.items():
        acct_dir = data_root / "accounts" / slug
        acct_dir.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (acct_dir / "gmail-intel.md").write_text(content, encoding="utf-8")
    return data_root


@contextmanager
def _patch_accounts(slugs: list[str], accounts_config: dict[str, Any] | None = None) -> Iterator[None]:
    """Patch account names and configuration for isolated signal detection tests."""
    accounts_config = accounts_config if accounts_config is not None else {slug: {} for slug in slugs}
    with (
        patch("fieldkit.watch.morning_brief_collect.get_account_names", return_value=slugs),
        patch(
            "fieldkit.watch.morning_brief_collect.get_accounts_config",
            return_value={"accounts": accounts_config},
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# detect_cross_account_signals
# ---------------------------------------------------------------------------


def test_detect_no_accounts(tmp_path: Path) -> None:
    """Empty account list → no signals."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    with _patch_accounts([]):
        signals = detect_cross_account_signals(data_root)
    assert signals == []


def test_detect_single_account(tmp_path: Path) -> None:
    """Single account with a keyword → returns [] (need ≥2)."""
    content = "## OpenShift Migration\n\nSome text here."
    data_root = _make_accounts(tmp_path, {"acme": content})
    with _patch_accounts(["acme"]):
        signals = detect_cross_account_signals(data_root)
    assert signals == []


def test_detect_shared_topic(tmp_path: Path) -> None:
    """Two accounts sharing a body keyword → signal returned with both accounts.

    historic regression update: each account must have the keyword ≥2 times (min-freq gate).
    """
    # 'openshift' appears twice per account to satisfy the ≥2 min-frequency gate
    content_a = "## Recent Activity\n\nOpenShift migration is underway. openshift cluster ready."
    content_b = "## Updates\n\nOpenShift upgrade completed. openshift nodes healthy."
    data_root = _make_accounts(tmp_path, {"acme": content_a, "globex": content_b})
    with _patch_accounts(["acme", "globex"]):
        signals = detect_cross_account_signals(data_root)

    topics = [s["topic"] for s in signals]
    assert "openshift" in topics
    sig = next(s for s in signals if s["topic"] == "openshift")
    assert sorted(sig["accounts"]) == ["acme", "globex"]
    # Contract: every signal must have required keys and valid types
    for signal in signals:
        assert "topic" in signal
        assert "accounts" in signal
        assert isinstance(signal["topic"], str)
        assert isinstance(signal["accounts"], list)
        assert len(signal["accounts"]) >= 2, "signals must appear in at least 2 accounts"


def test_detect_self_identity_token_plus_one_other_account_is_not_a_signal(tmp_path: Path) -> None:
    """An account's slug tokens do not count toward cross-account quorum."""
    data_root = _make_accounts(
        tmp_path,
        {
            "acme-corp": "Acme rollout is planned. acme stakeholders are aligned.",
            "globex": "Acme is a competitor. acme pricing was discussed.",
        },
    )

    with _patch_accounts(["acme-corp", "globex"]):
        signals = detect_cross_account_signals(data_root)

    assert signals == []


def test_detect_independent_shared_topic_is_preserved(tmp_path: Path) -> None:
    """A non-identity topic occurring twice in two eligible accounts is retained."""
    data_root = _make_accounts(
        tmp_path,
        {
            "acme-corp": "Kubernetes rollout is planned. kubernetes cluster is ready.",
            "globex": "Kubernetes adoption is expanding. kubernetes migration is approved.",
        },
    )

    with _patch_accounts(["acme-corp", "globex"]):
        signals = detect_cross_account_signals(data_root)

    assert signals == [{"topic": "kubernetes", "accounts": ["acme-corp", "globex"]}]


def test_detect_internal_account_does_not_contribute_to_quorum(tmp_path: Path) -> None:
    """An internal account's matching topic cannot create a two-account signal."""
    data_root = _make_accounts(
        tmp_path,
        {
            "internal-team": "Kubernetes rollout is planned. kubernetes cluster is ready.",
            "globex": "Kubernetes adoption is expanding. kubernetes migration is approved.",
        },
    )

    with _patch_accounts(["internal-team", "globex"], {"internal-team": {"internal": True}, "globex": {}}):
        signals = detect_cross_account_signals(data_root)

    assert signals == []


def test_detect_stopword_filtered(tmp_path: Path) -> None:
    """A word that is in _STOPWORDS is not returned as a signal."""
    # "support" is a stopword; "review" is a stopword
    content_a = "Support update text here."
    content_b = "Support review notes."
    data_root = _make_accounts(tmp_path, {"acme": content_a, "globex": content_b})
    with _patch_accounts(["acme", "globex"]):
        signals = detect_cross_account_signals(data_root)

    topics = [s["topic"] for s in signals]
    assert "support" not in topics
    assert "update" not in topics
    assert "review" not in topics


def test_detect_short_keyword_filtered(tmp_path: Path) -> None:
    """Words shorter than 5 chars are not returned as signals."""
    content_a = "API bug fix applied."
    content_b = "API new feature shipped."
    data_root = _make_accounts(tmp_path, {"acme": content_a, "globex": content_b})
    with _patch_accounts(["acme", "globex"]):
        signals = detect_cross_account_signals(data_root)

    topics = [s["topic"] for s in signals]
    # "api", "bug", "fix", "new" are all < 5 chars
    for short in ("api", "bug", "fix", "new"):
        assert short not in topics


def test_detect_no_intel_files(tmp_path: Path) -> None:
    """Accounts with no gmail-intel.md → returns []."""
    data_root = _make_accounts(tmp_path, {"acme": None, "globex": None})
    with _patch_accounts(["acme", "globex"]):
        signals = detect_cross_account_signals(data_root)
    assert signals == []


def test_detect_sorted_by_account_count(tmp_path: Path) -> None:
    """Signals are sorted descending by number of accounts.

    historic regression update: each account must have the keyword ≥2 times (min-freq gate).
    """
    # "kubernetes" appears ≥2 times in 3 accounts; "ansible" ≥2 times in 2 accounts
    k_content = "Kubernetes cluster ongoing. kubernetes nodes ready.\n"
    # Use "terraform" as the secondary keyword — not in stoplist, product-specific
    a_content = "Terraform modules deployed. terraform configuration updated.\n"
    data_root = _make_accounts(
        tmp_path,
        {
            "acme": k_content + a_content,
            "globex": k_content + a_content,
            "initech": k_content,
        },
    )
    with _patch_accounts(["acme", "globex", "initech"]):
        signals = detect_cross_account_signals(data_root)

    # kubernetes (3 accounts) should come before terraform (2 accounts)
    topics = [s["topic"] for s in signals]
    assert "kubernetes" in topics
    assert "terraform" in topics
    assert topics.index("kubernetes") < topics.index("terraform")


def test_detect_unreadable_intel_file_skipped(tmp_path: Path) -> None:
    """An OSError on reading gmail-intel.md is caught and the account is skipped."""
    data_root = _make_accounts(tmp_path, {"acme": "## OpenShift\n\nOpenShift OpenShift."})
    # Make the file unreadable
    intel_file = data_root / "accounts" / "acme" / "gmail-intel.md"
    intel_file.chmod(0o000)
    try:
        with _patch_accounts(["acme"]):
            signals = detect_cross_account_signals(data_root)
        # Account should be skipped, no signals
        assert signals == []
    finally:
        intel_file.chmod(0o644)


# ---------------------------------------------------------------------------
# _render_cross_account_section
# ---------------------------------------------------------------------------


def test_render_cross_account_section_empty() -> None:
    """No signals → empty list."""
    assert _render_cross_account_section([]) == []


def test_render_cross_account_section() -> None:
    """Signals render into expected markdown."""
    signals: list[dict[str, Any]] = [
        {"topic": "openshift", "accounts": ["acme", "globex"]},
    ]
    lines = _render_cross_account_section(signals)

    output = "\n".join(lines)
    assert "## Cross-Account Signals" in output
    assert "openshift" in output
    assert "acme" in output
    assert "globex" in output


# ---------------------------------------------------------------------------
# render_brief integration
# ---------------------------------------------------------------------------


def _minimal_brief_kwargs() -> dict[str, Any]:
    """Return minimal keyword args to call render_brief without errors."""
    from datetime import date as _date

    return {
        "target_date": _date.today(),
        "meetings": [],
        "backstory_alerts": [],
        "pursuit_stall_alerts": [],
        "slack_alerts": [],
        "pipeline_review_md": "",
        "elapsed_seconds": 0.0,
        "quota_collector": lambda _root: [],
        "cross_account_signals": [],
    }


def test_render_brief_includes_cross_account_section(tmp_path) -> None:
    """render_brief with signals includes '## Cross-Account Signals'."""
    signals: list[dict[str, Any]] = [
        {"topic": "kubernetes", "accounts": ["acme", "globex"]},
    ]
    kwargs = _minimal_brief_kwargs()
    kwargs["cross_account_signals"] = signals

    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path):
        output = render_brief(**kwargs)
    assert "## Cross-Account Signals" in output
    assert "kubernetes" in output


def test_render_brief_omits_section_when_empty(tmp_path) -> None:
    """render_brief with no signals omits the '## Cross-Account Signals' section."""
    kwargs = _minimal_brief_kwargs()
    with patch("fieldkit.watch.morning_brief_render.get_fieldkit_home", return_value=tmp_path):
        output = render_brief(**kwargs)
    assert "## Cross-Account Signals" not in output
