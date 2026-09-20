#!/usr/bin/env python3
"""Observe a published candidate from a public package index without publishing."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts import release_consumer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--index-endpoint", required=True)
    parser.add_argument("--download-host", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--allow-live-index", action="store_true")
    parser.add_argument("--verify-downloads", action="store_true")
    parser.add_argument("--verify-install", action="store_true")
    return parser


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("evidence output parent must be a directory")
    encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".release-consumer-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.allow_live_index:
        print("Release consumer: ERROR: --allow-live-index is required", file=sys.stderr)
        return 2
    if args.verify_downloads and args.download_dir is None:
        print("Release consumer: ERROR: --download-dir is required with --verify-downloads", file=sys.stderr)
        return 2
    if args.verify_install and not args.verify_downloads:
        print("Release consumer: ERROR: --verify-install requires --verify-downloads", file=sys.stderr)
        return 2
    try:
        expected = release_consumer.expected_release(args.bundle, args.candidate_report)
    except (OSError, ValueError) as error:
        print(f"Release consumer: ERROR: {error}", file=sys.stderr)
        return 3
    observed: tuple[release_consumer.ObservedArtifact, ...] = ()
    scenarios: tuple[dict[str, object], ...] = ()
    system = platform.system().lower()
    machine = platform.machine().lower()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        allowed_hosts = frozenset(args.download_host)
        poll_result = release_consumer.poll_index_observations(
            expected,
            fetch=lambda: release_consumer.fetch_index_observations(
                args.index_endpoint,
                timeout_seconds=args.timeout_seconds,
                allowed_download_hosts=allowed_hosts,
            ),
            max_attempts=args.max_attempts,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        observed = poll_result.observed
        report = poll_result.report
        downloaded: tuple[release_consumer.DownloadedArtifact, ...] = ()
        if report.ok and args.verify_downloads:
            if args.download_dir.parent.is_symlink():
                raise ValueError("download destination parent must not be a symlink")
            args.download_dir.mkdir(mode=0o700, exist_ok=True)
            downloaded = release_consumer.download_expected_artifacts(
                expected,
                observed,
                args.download_dir,
                fetch=lambda url, timeout: release_consumer.fetch_https_bytes(
                    url, timeout_seconds=timeout, allowed_download_hosts=allowed_hosts
                ),
                timeout_seconds=args.timeout_seconds,
            )
            if args.verify_install:
                downloaded_by_name = {artifact.path.name: artifact for artifact in downloaded}
                scenarios = tuple(
                    scenario
                    for artifact in expected.artifacts
                    for scenario in release_consumer.offline_scenario_evidence(
                        release_consumer.run_offline_scenarios(
                            args.bundle,
                            args.candidate_report,
                            downloaded_by_name[artifact.name],
                            artifact,
                            system=system,
                            machine=machine,
                            python_version=python_version,
                        ),
                        artifact_name=artifact.name,
                    )
                )
    except subprocess.TimeoutExpired:
        report = release_consumer.IndexReport("failed", ("offline verification timed out",))
        downloaded = ()
        scenarios = ()
    except (OSError, ValueError) as error:
        report = release_consumer.IndexReport("failed", (str(error),))
        downloaded = ()
    try:
        evidence = release_consumer.consumer_evidence(
            expected,
            endpoint=args.index_endpoint,
            observed=observed,
            report=report,
            system=system,
            machine=machine,
            python_version=python_version,
            downloaded=downloaded,
            scenarios=scenarios,
        )
        _write_evidence(args.output, evidence)
    except (OSError, ValueError) as error:
        print(f"Release consumer: ERROR: {error}", file=sys.stderr)
        return 3
    status = evidence["status"]
    if not isinstance(status, str):
        print("Release consumer: ERROR: consumer evidence has an invalid status", file=sys.stderr)
        return 3
    print(f"Release consumer: {status.upper()} ({args.output})")
    return 0 if status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
