#!/usr/bin/env python3
"""Validate the OpenChamber developer-schedule admission policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fieldkit.driver.admission import ADMITTED_JOBS

_PROVIDER = "openai"
_MODEL = "gpt-5.6-terra"
_PREFLIGHT_MARKER = "zero-LLM scheduler preflight already acquired"


def _errors(data: dict[str, Any]) -> list[str]:
    tasks = data.get("scheduledTasks", [])
    if not isinstance(tasks, list):
        return ["scheduledTasks must be a list"]
    if not tasks:
        return ["scheduledTasks must not be empty"]

    errors: list[str] = []
    enabled = [task for task in tasks if isinstance(task, dict) and task.get("enabled") is True]
    if len(enabled) > 1:
        errors.append("at most one developer schedule may be enabled")

    for task in tasks:
        if not isinstance(task, dict):
            errors.append("each scheduled task must be an object")
            continue
        name = task.get("name")
        execution = task.get("execution")
        if not isinstance(name, str) or not isinstance(execution, dict):
            errors.append("each scheduled task requires name and execution")
            continue
        if not isinstance(task.get("enabled"), bool):
            errors.append(f"{name}: enabled must be explicitly true or false")
        if name not in ADMITTED_JOBS:
            errors.append(f"{name}: job is not approved for developer admission")
        if execution.get("providerID") != _PROVIDER:
            errors.append(f"{name}: provider must be {_PROVIDER}")
        if execution.get("modelID") != _MODEL:
            errors.append(f"{name}: model must be {_MODEL}")
        if task.get("enabled") is True:
            prompt = execution.get("prompt")
            required = f"fieldkit driver admit --job {name} --json"
            release = f"fieldkit driver admit --job {name} --release --json"
            preflight_gated = isinstance(prompt, str) and _PREFLIGHT_MARKER in prompt
            if not isinstance(prompt, str) or release not in prompt or (required not in prompt and not preflight_gated):
                errors.append(f"{name}: enabled schedule must acquire and release its admission lease")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="OpenChamber project JSON file")
    args = parser.parse_args()
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read schedule config: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("schedule config root must be an object", file=sys.stderr)
        return 1
    errors = _errors(data)
    if errors:
        print("developer schedule policy: FAIL", file=sys.stderr)
        print(*[f"- {error}" for error in errors], sep="\n", file=sys.stderr)
        return 1
    print("developer schedule policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
