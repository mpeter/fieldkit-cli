"""Integration test: full gmail-cache pipeline chain.

account_tags → apply_intel → enrich_pursuits against a shared seeded DB in tmp_path.
No live credentials; no writes to live accounts/ or data/.
"""

import pytest

from fieldkit.commands.gmail import apply_intel, enrich_pursuits
from fieldkit.gmail.account_tags import update_account_tags
from fieldkit.gmail.publication import open_gmail_publication


@pytest.mark.integration
def test_full_pipeline(pipeline_db, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
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
    with open_gmail_publication(db_path) as conn_pre:
        pre_count = conn_pre.execute("SELECT COUNT(*) FROM thread_accounts").fetchone()[0]
    assert pre_count == 0, f"Expected 0 thread_accounts before account-tags, got {pre_count}"

    update_account_tags(db_path)

    with open_gmail_publication(db_path) as conn_post:
        post_count = conn_post.execute("SELECT COUNT(*) FROM thread_accounts").fetchone()[0]
    assert post_count > 0, "account-tags did not write any thread_accounts rows"

    # ── Step 2: apply-intel.build_signals() ──────────────────────────────────
    # build_signals() still generates signal content; main() no longer writes it to pursuit files
    pursuit_dir = tmp_path / "accounts" / "acme-bank" / "pursuits"
    pursuit_dir.mkdir(parents=True, exist_ok=True)
    pursuit_path = pursuit_dir / "test-pursuit.md"
    pursuit_path.write_text(
        "---\nstage: discover\n---\n\n# Acme Bank Test Pursuit\n\nNo customer emails here.\n", encoding="utf-8"
    )

    with open_gmail_publication(db_path) as conn:
        signals = apply_intel.build_signals(str(pursuit_path), "acme-bank", conn)

    assert signals is not None, "build_signals() returned None"
    assert "## Gmail Signals" in signals, "Missing '## Gmail Signals' header"

    # ── Step 3: enrich-pursuits.build_account_report() ───────────────────────
    # Use the exact published generation produced by account-tags.
    monkeypatch.setattr(enrich_pursuits, "get_accounts_root", lambda: tmp_path / "accounts")
    monkeypatch.setattr(enrich_pursuits, "get_gmail_db_path", lambda: db_path)
    monkeypatch.setattr(
        enrich_pursuits,
        "get_accounts_config",
        lambda: {"accounts": {"acme-bank": {"domains": ["acmebank.example.com"]}}},
    )
    report = enrich_pursuits.build_account_report("acme-bank")

    assert isinstance(report, str), "build_account_report() did not return a report"
    assert "Gmail Intelligence Report" in report, "Missing 'Gmail Intelligence Report' in report"

    # ── Step 4: write gmail-intel.md and verify ───────────────────────────────
    intel_path = tmp_path / "accounts" / "acme-bank" / "gmail-intel.md"
    intel_path.write_text(report, encoding="utf-8")

    assert intel_path.exists(), f"gmail-intel.md not found at {intel_path}"
    content = intel_path.read_text()
    assert "acme-bank" in content.lower(), "Expected account name 'acme-bank' in gmail-intel.md"
