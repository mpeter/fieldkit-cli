#!/usr/bin/env python3
"""Read-only verification of live GitHub repository settings against policy."""

import argparse
import json
import sys
from pathlib import Path

from _repository_settings import collect, evaluate_snapshot, load_manifest, load_snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / ".github" / "repository-settings.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", nargs="?", help="GitHub repository as OWNER/REPO")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path, help="verify an offline API snapshot instead of calling GitHub")
    parser.add_argument("--phase", choices=("pre-cutover", "post-cutover"), default="pre-cutover")
    parser.add_argument("--expected-revision", help="exact public main-branch SHA required after cutover")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        if args.repository is None:
            raise ValueError("repository is required")
        snapshot = load_snapshot(args.snapshot, args.repository) if args.snapshot else collect(args.repository)
        report = evaluate_snapshot(
            manifest,
            snapshot,
            phase=args.phase,
            expected_revision=args.expected_revision,
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"repository settings verification error: {exc}\n")
        return 3

    payload = {
        **report.to_dict(),
        "repository": snapshot.repository,
        "collected_at": snapshot.collected_at,
        "api_version": snapshot.api_version,
        "default_branch_sha": snapshot.default_branch_sha,
    }
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
