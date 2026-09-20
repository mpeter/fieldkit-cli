#!/usr/bin/env python3
"""Check the Gmail sync partial-fetch contract using candidate source text."""

from __future__ import annotations

import sys
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {path}: {exc}")
        return None


def main(argv: list[str]) -> int:
    if len(argv) < 6:
        print("usage: check_gmail_sync_partial.py ERRORS CLI_EXIT SYNC RETRY BATCH TESTS [TESTS ...]")
        return 1
    loaded = [_read(path) for path in argv]
    if any(text is None for text in loaded):
        return 1
    errors, cli_exit, sync, retry, batch = loaded[:5]
    tests = "\n".join(text for text in loaded[5:] if text is not None)
    assert errors is not None and cli_exit is not None and sync is not None and retry is not None and batch is not None

    required = {
        "GmailSyncPartialError exception": "class GmailSyncPartialError" in errors,
        "partial exit mapping": any(
            marker in cli_exit
            for marker in (
                "isinstance(exc, GmailSyncPartialError)",
                "isinstance(exc, (FrontmatterStalenessError, GmailSyncPartialError))",
            )
        ),
        "typed batch result": "class BatchFetchResult" in batch,
        "not-found count": "not_found" in batch,
        "unresolved count": "unresolved" in batch,
        "auth propagation": "GmailAuthError" in sync,
        "shared auth normalization": "_normalize_gmail_http_error" in retry,
        "full-sync checkpoint regression": "test_full_sync_unresolved_fetch_retains_current_page" in tests,
        "incremental checkpoint regression": "test_incremental_sync_unresolved_fetch_retains_history_id" in tests,
        "full-sync 404 advancement regression": "test_full_sync_not_found_advances_to_next_page" in tests,
        "incremental 404 advancement regression": "test_incremental_sync_not_found_advances_history_id" in tests,
        "missing callback regression": "test_batch_missing_callback_payload_is_unresolved" in tests,
        "callback auth regression": "test_batch_auth_failure_raises_gmail_auth_error" in tests,
        "batch refresh auth regression": "test_batch_refresh_failure_raises_gmail_auth_error" in tests,
        "full-sync earliest-gap regression": "test_full_sync_unresolved_fetch_stops_before_later_chunk" in tests,
        "incremental earliest-gap regression": "test_incremental_sync_unresolved_fetch_stops_before_next_page" in tests,
        "second-chunk auth regression": "test_full_sync_second_chunk_auth_retains_current_page" in tests,
        "capped-run progress regression": "test_full_sync_consecutive_capped_runs_advance_pages" in tests,
        "processed history boundary regression": "test_incremental_sync_uses_final_history_response_id" in tests,
        "full list auth regression": "test_full_sync_list_auth_raises_gmail_auth_error" in tests,
        "full profile auth regression": "test_full_sync_profile_auth_raises_gmail_auth_error" in tests,
        "incremental profile boundary regression": "test_incremental_missing_history_does_not_query_profile" in tests,
        "partial exit regression": "test_cli_mixed_fetch_outcome_exits_partial" in tests,
        "redaction regression": "test_batch_failure_logs_exclude_message_ids" in tests,
    }
    failures = [name for name, present in required.items() if not present]
    if "Batch fetch failed for message %s" in sync:
        failures.append("identifier-bearing batch failure log remains")
    if failures:
        for failure in failures:
            print(f"Gmail partial contract: {failure}")
        return 1
    print("Gmail partial contract: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
