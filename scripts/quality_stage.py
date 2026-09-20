"""Run one quality stage with labelled diagnostics and timing provenance."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from semantic_python_changes import has_semantic_python_changes

from fieldkit.config import TIMEOUT_HEALTH_GATE

QUALITY_STAGE_TIMEOUT = 120
"""Bounded developer and pull-request quality stage ceiling in seconds."""


def _revision() -> str | None:
    """Return the checked-out revision when this is a Git worktree."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _write_diagnostics(label: str, stream: TextIO, output: str) -> None:
    """Replay complete stage output with a stream-specific label."""
    if not output:
        return
    for line in output.splitlines(keepends=True):
        stream.write(f"[quality:{label}] {line}")


def _text_output(output: str | bytes | None) -> str:
    """Normalize subprocess output retained by a timeout exception."""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _combined_timeout_output(partial: str | bytes | None, final: str | bytes | None) -> str:
    """Retain timeout output whether communicate returns a full buffer or remainder."""
    partial_text = _text_output(partial)
    final_text = _text_output(final)
    if final_text.startswith(partial_text):
        return final_text
    if partial_text.endswith(final_text):
        return partial_text
    return partial_text + final_text


def main(argv: list[str]) -> int:
    """Execute a command and emit its timing record as one JSON line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--quality-base")
    parser.add_argument("--full-enforcement", action="store_true")
    parser.add_argument("--skip-when-docs-only", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a stage command is required after --")

    revision = _revision()
    if args.skip_when_docs_only and args.quality_base and revision is not None:
        repo_root = Path(__file__).resolve().parent.parent
        if not has_semantic_python_changes(args.quality_base, revision, repo_root):
            print(f"[quality:{args.label}:stdout] Skipped: changed Python source is comments/docstrings only.")
            print(
                json.dumps(
                    {
                        "argv": command,
                        "elapsed_seconds": 0.0,
                        "environment": {},
                        "exit_status": 0,
                        "revision": {"head": revision, "quality_base": args.quality_base},
                        "stage": args.label,
                        "status": "skipped-docs-only",
                    },
                    sort_keys=True,
                )
            )
            return 0

    environment = os.environ.copy()
    stage_environment: dict[str, str] = {}
    for assignment in args.env:
        key, separator, value = assignment.partition("=")
        if not key or not separator:
            parser.error(f"--env requires KEY=VALUE, got {assignment!r}")
        environment[key] = value
        stage_environment[key] = value

    started = time.monotonic()
    timeout = TIMEOUT_HEALTH_GATE if args.full_enforcement else QUALITY_STAGE_TIMEOUT
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        exit_status = process.returncode
    except subprocess.TimeoutExpired as error:
        if process is None:
            raise
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        final_stdout, final_stderr = process.communicate()
        stdout = _combined_timeout_output(error.stdout, final_stdout)
        stderr = _combined_timeout_output(error.stderr, final_stderr) or f"{error}\n"
        exit_status = 124
    except OSError as error:
        stdout = ""
        stderr = f"{error}\n"
        exit_status = 127
    elapsed_seconds = time.monotonic() - started
    _write_diagnostics(f"{args.label}:stdout", sys.stdout, stdout)
    _write_diagnostics(f"{args.label}:stderr", sys.stderr, stderr)
    record = {
        "stage": args.label,
        "argv": command,
        "elapsed_seconds": elapsed_seconds,
        "exit_status": exit_status,
        "revision": {"head": _revision(), "quality_base": args.quality_base},
        "environment": stage_environment,
    }
    print(json.dumps(record, sort_keys=True))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
