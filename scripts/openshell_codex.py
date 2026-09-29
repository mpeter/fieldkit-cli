"""Run a coding worker with the existing subscription login and no refresh-token copy."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path

import click
import openshell_workspace as workspace

PROFILE = "fieldkit-codex"
CONTROL_TIMEOUT = 60


def verify_profile(profile: object) -> None:
    if not isinstance(profile, dict):
        raise ValueError("Codex profile must be an object")
    credentials = profile.get("credentials")
    if (
        profile.get("id") != PROFILE
        or profile.get("scope") != "workspace"
        or profile.get("endpoints")
        != [{"host": "chatgpt.com", "port": 443, "protocol": "rest", "access": "read-write", "enforcement": "enforce"}]
        or profile.get("binaries") != ["/usr/local/bin/codex"]
        or not isinstance(credentials, list)
        or len(credentials) != 1
        or not isinstance(credentials[0], dict)
        or credentials[0].get("env_vars") != ["CODEX_AUTH_ACCESS_TOKEN"]
        or credentials[0].get("auth_style") != "bearer"
        or credentials[0].get("header_name") != "authorization"
        or credentials[0].get("required") is not True
        or credentials[0].get("refresh")
        or profile.get("refresh")
    ):
        raise ValueError("Codex profile differs from the approved token-only, single-endpoint profile")


def execute(project: Path, inputs: list[str], output: str, command: list[str], timeout: int) -> int:
    manifest = workspace.load_manifest(project, ".openshell/workspace.json")
    if manifest.workspace != "fieldkit-cli" or manifest.snapshot != "git-head":
        raise ValueError("the subscription adapter requires the CLI source workspace")
    prefix = ["openshell", "--workspace", manifest.workspace]
    profile = workspace.run([*prefix, "profile", "export", PROFILE, "-o", "json"], timeout=CONTROL_TIMEOUT)
    verify_profile(json.loads(profile.stdout))
    auth: object = json.loads((Path.home() / ".codex/auth.json").read_text(encoding="utf-8"))
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        raise ValueError("an existing Codex subscription login is required")
    tokens = auth["tokens"]
    bearer, account = tokens.get("access_token"), tokens.get("account_id")
    if not isinstance(bearer, str) or not bearer or not isinstance(account, str) or not account:
        raise ValueError("the existing login lacks access or account routing metadata")
    provider = f"codex-{uuid.uuid4().hex}"
    env = dict(os.environ)
    env["CODEX_AUTH_ACCESS_TOKEN"] = bearer
    primary_error: BaseException | None = None
    cleanup_error: Exception | None = None
    code = 1
    try:
        workspace.run(
            [
                *prefix,
                "provider",
                "create",
                "--name",
                provider,
                "--type",
                PROFILE,
                "--credential",
                "CODEX_AUTH_ACCESS_TOKEN",
            ],
            timeout=CONTROL_TIMEOUT,
            env=env,
        )
        args = [
            "--project",
            str(project),
            "--provider",
            provider,
            "--output",
            output,
            "--timeout",
            str(timeout),
            "--env",
            f"CODEX_ACCOUNT_ID={account}",
        ]
        for item in inputs:
            args.extend(["--input", item])
        code = workspace.main([*args, "--", "/usr/local/bin/codex-with-provider", *command])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        primary_error = exc
    finally:
        with workspace.defer_cancellation():
            env.pop("CODEX_AUTH_ACCESS_TOKEN", None)
            try:
                workspace.run([*prefix, "provider", "delete", provider], timeout=CONTROL_TIMEOUT)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                cleanup_error = exc
            if cleanup_error:
                primary = type(primary_error).__name__ if primary_error else f"worker exit {code}"
                raise RuntimeError(
                    f"provider cleanup failed for {provider}; primary outcome: {primary}"
                ) from cleanup_error
    if primary_error:
        raise primary_error
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--output", default=f".openshell/runs/{uuid.uuid4().hex}")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    previous_signal = signal.signal(signal.SIGTERM, workspace.cancel_workspace)
    try:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command or not 0 < args.timeout <= 600:
            raise ValueError("provide Codex arguments and a timeout of at most 600 seconds")
        return execute(args.project.resolve(strict=True), args.input, args.output, command, args.timeout)
    except subprocess.TimeoutExpired:
        click.echo("Codex worker: a bounded credential operation timed out", err=True)
        return 1
    except KeyboardInterrupt:
        click.echo("Codex worker interrupted; cleanup attempted", err=True)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        click.echo(f"Codex worker: {exc}", err=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_signal)


if __name__ == "__main__":
    raise SystemExit(main())
