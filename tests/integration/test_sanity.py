"""Sanity test: verifies the seeded_db fixture works end-to-end."""

import sqlite3

import pytest


@pytest.mark.integration
def test_seeded_db_fixture(seeded_db):
    """DB file exists in tmp_path; all 5 schema tables present; exact seed row counts."""
    assert seeded_db.exists(), f"DB file not found: {seeded_db}"

    conn = sqlite3.connect(str(seeded_db))
    try:
        # All 5 schema tables must be present and have correct row counts
        expected = {
            "threads": 3,
            "messages": 5,
            "thread_accounts": 2,
            "people": 3,
            "labels": 0,
        }
        for table, expected_count in expected.items():
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert count == expected_count, f"Table '{table}': expected {expected_count} rows, got {count}"
    finally:
        conn.close()
