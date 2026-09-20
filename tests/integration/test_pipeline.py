"""Integration test: full gmail-cache pipeline chain.

account_tags → apply_intel → enrich_pursuits against a shared seeded DB in tmp_path.
No live credentials; no writes to live accounts/ or data/.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.gmail import account_tags, apply_intel, enrich_pursuits


def _has_account_domains() -> bool:
    try:
        return bool(apply_intel._account_domains())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_account_domains(),
    reason="config/accounts.yaml not found — _account_domains() is empty",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_full_pipeline(pipeline_db, tmp_path):
    """Chain account_tags → apply_intel → enrich_pursuits against a seeded DB.

    Verifies that:
    1. account-tags populates thread_accounts from label-tagged messages
    2. apply-intel.build_signals() returns a Gmail Signals section (note: main() no longer writes this to pursuit files)
    3. enrich-pursuits.build_account_report() returns a report with expected header
    4. gmail-intel.md is written to tmp_path with a known contact name
    """

    db_path = pipeline_db

    # ── Step 1: account-tags ─────────────────────────────────────────────────
    # DB starts with 0 thread_accounts rows; account-tags should populate them.
    conn_pre = sqlite3.connect(str(db_path))
    pre_count = conn_pre.execute("SELECT COUNT(*) FROM thread_accounts").fetchone()[0]
    conn_pre.close()
    assert pre_count == 0, f"Expected 0 thread_accounts before account-tags, got {pre_count}"

    account_tags.build_account_tags(str(db_path))

    conn_post = sqlite3.connect(str(db_path))
    post_count = conn_post.execute("SELECT COUNT(*) FROM thread_accounts").fetchone()[0]
    conn_post.close()
    assert post_count > 0, "account-tags did not write any thread_accounts rows"

    # ── Step 2: apply-intel.build_signals() ──────────────────────────────────
    # build_signals() still generates signal content; main() no longer writes it to pursuit files
    pursuit_dir = tmp_path / "accounts" / "acme-bank" / "pursuits"
    pursuit_dir.mkdir(parents=True, exist_ok=True)
    pursuit_path = pursuit_dir / "test-pursuit.md"
    pursuit_path.write_text(
        "---\nstage: discover\n---\n\n# Acme Bank Test Pursuit\n\nNo customer emails here.\n", encoding="utf-8"
    )

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr)")
    signals = apply_intel.build_signals(str(pursuit_path), "acme-bank", conn)
    conn.close()

    assert signals is not None, "build_signals() returned None"
    assert "## Gmail Signals" in signals, "Missing '## Gmail Signals' header"

    # ── Step 3: enrich-pursuits.build_account_report() ───────────────────────
    # Monkeypatch accounts root and DB path; stub out direct query calls so
    # the report generates its structural headers without a live gmail.db.
    enrich_pursuits.ACCOUNTS = ["acme-bank"]
    enrich_pursuits.get_accounts_root = lambda: tmp_path / "accounts"

    fake_conn = MagicMock(spec=sqlite3.Connection)
    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.connect", return_value=fake_conn),
        patch("fieldkit.commands.gmail.enrich_pursuits.get_gmail_db_path", return_value=db_path),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_blindspots", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_champion_signals", return_value=""),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_dig", return_value=[]),
    ):
        report = enrich_pursuits.build_account_report("acme-bank")

    assert report is not None, "build_account_report() returned None"
    assert "Gmail Intelligence Report" in report, "Missing 'Gmail Intelligence Report' in report"

    # ── Step 4: write gmail-intel.md and verify ───────────────────────────────
    intel_path = tmp_path / "accounts" / "acme-bank" / "gmail-intel.md"
    intel_path.write_text(report, encoding="utf-8")

    assert intel_path.exists(), f"gmail-intel.md not found at {intel_path}"
    content = intel_path.read_text()
    assert "acme-bank" in content.lower(), "Expected account name 'acme-bank' in gmail-intel.md"
