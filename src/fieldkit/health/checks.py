"""fieldkit.health.checks — the per-check gate table and single-check runner.

The table mirrors the ``quality`` recipe in the repository Makefile, in recipe
order (order matters: ``pytest-cov`` writes ``coverage.json``, which the gazepy
checks read). The Makefile is the source of truth for gate definitions and
thresholds — when the recipe changes, this table must track it
(``test_health_sensor.py`` pins the vocabulary). The sensor never moves a gate:
the thresholds below are copies of the frozen values, present only so each gate
can be invoked individually for clean per-check attribution (design Decision 3).
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from fieldkit.config import TIMEOUT_HEALTH_GATE

log = logging.getLogger(__name__)

#: Maximum characters of combined check output kept for an issue body.
MAX_OUTPUT_CHARS = 8_000

_MAKEFILE_CRAPLOAD_MARKER = "{makefile-crapload}"
_MAX_CRAPLOAD_RE = re.compile(r"--max-crapload\s+(\d+)")


@dataclass(frozen=True)
class HealthCheck:
    """One individually-invocable quality gate.

    Attributes:
        check_id: Stable slug — the dedup vocabulary. Renaming one orphans its
            open issues; treat as a contract.
        argv:     Command to run (no shell). Elements containing ``*`` are
            glob-expanded relative to the working tree at run time.
        env:      Extra environment entries for this check.
        optional: True for checks whose harness may be absent; an invocation
            error degrades them to ``skipped`` instead of a runner error.
    """

    check_id: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    optional: bool = False


#: The gate bundle, mirroring ``make quality`` (minus the warning-only taxonomy
#: check, which cannot fail the bundle and so cannot regress).
HEALTH_CHECKS: tuple[HealthCheck, ...] = (
    HealthCheck("ruff-lint", ("uv", "run", "ruff", "check", ".")),
    HealthCheck("ruff-format", ("uv", "run", "ruff", "format", "--check", ".")),
    HealthCheck("mypy", ("uv", "run", "mypy", "src/fieldkit/", "hooks/*.py", "--no-error-summary")),
    HealthCheck("tach", ("uvx", "tach", "check")),
    HealthCheck(
        "skill-integrity",
        ("uv", "run", "python", "scripts/check_skill_integrity.py", "--report", "reports/skill-integrity.json"),
    ),
    HealthCheck("click-params", ("uv", "run", "python", "scripts/check_click_params.py", "src/fieldkit/commands/")),
    HealthCheck("cli-docs", ("uv", "run", "python", "scripts/generate_cli_docs.py", "--check")),
    HealthCheck("dep-map", ("uv", "run", "python", "scripts/generate_dep_map.py", "--check")),
    HealthCheck("doc-freshness", ("uv", "run", "python", "scripts/check_doc_freshness.py")),
    HealthCheck("schema-sync", ("uv", "run", "python", "scripts/check_schema_sync.py")),
    HealthCheck("skillsaw", ("uv", "run", "python", "scripts/check_skillsaw.py")),
    HealthCheck(
        "pytest-cov",
        (
            "uv",
            "run",
            "pytest",
            "tests/",
            "--cov",
            "--cov-report=term-missing",
            "--cov-report=json:coverage.json",
            "-q",
            "-n",
            "auto",
        ),
    ),
    HealthCheck(
        "gazepy-crap",
        (
            "uv",
            "run",
            "gazepy",
            "crap",
            "src/fieldkit/",
            "--coverprofile",
            "coverage.json",
            "--baseline",
            ".gaze/baseline.json",
            "--max-crapload",
            _MAKEFILE_CRAPLOAD_MARKER,
        ),
    ),
    HealthCheck(
        "gazepy-quality",
        ("uv", "run", "gazepy", "quality", "src/fieldkit/", "--min-contract-coverage", "50"),
    ),
    HealthCheck("agentready-assess", ("uv", "run", "python", "scripts/agentready_assess.py", "agentready==2.49.0")),
    HealthCheck("agentready-gate", ("uv", "run", "python", "scripts/check_agentready.py")),
    HealthCheck(
        "skill-eval-behavioral",
        ("uv", "run", "fieldkit", "skill", "eval", "--behavioral", "--all"),
        env=(("NO_LLM", "1"),),
        optional=True,
    ),
)

CheckStatus = Literal["pass", "fail", "error", "skipped"]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one gate invocation.

    ``fail`` is a gate verdict (the check ran and found a regression);
    ``error`` means the check could not be *executed* (tool crash, timeout) —
    a runner-level problem, never filed as a regression.
    """

    check_id: str
    status: CheckStatus
    output: str
    elapsed_seconds: float


def _makefile_crapload(cwd: Path) -> str:
    """Return the one baseline-free Gazepy CRAP ceiling defined by ``cwd``'s Makefile."""
    makefile = cwd / "Makefile"
    try:
        text = makefile.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read Gazepy ceiling from {makefile}: {exc}") from exc

    lines = re.sub(r"\\\n[ \t]*", " ", text).splitlines()
    ceilings = {
        match.group(1)
        for line in lines
        if not line.lstrip().startswith("#") and "uv run gazepy crap" in line and "--baseline" not in line
        for match in [_MAX_CRAPLOAD_RE.search(line)]
        if match is not None
    }
    if len(ceilings) != 1:
        raise ValueError(
            f"expected one baseline-free Gazepy --max-crapload value in {makefile}, got {sorted(ceilings)}"
        )
    return ceilings.pop()


def _resolve_marker(part: str, cwd: Path) -> str:
    """Resolve a worktree-derived argument marker without changing ordinary args."""
    return _makefile_crapload(cwd) if part == _MAKEFILE_CRAPLOAD_MARKER else part


def _expand_argv(argv: tuple[str, ...], cwd: Path) -> list[str]:
    """Expand worktree-derived markers and globs in a check argv without a shell."""
    expanded: list[str] = []
    for part in argv:
        resolved = _resolve_marker(part, cwd)
        if "*" in resolved:
            matches = sorted(cwd.glob(resolved))
            expanded.extend(str(m.relative_to(cwd)) for m in matches)
        else:
            expanded.append(resolved)
    return expanded


def _tail(text: str) -> str:
    """Return the last ``MAX_OUTPUT_CHARS`` of *text* (issue bodies stay bounded)."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[-MAX_OUTPUT_CHARS:]


def run_check(check: HealthCheck, *, cwd: Path, env: dict[str, str]) -> CheckResult:
    """Run one gate in *cwd* and classify the outcome.

    Args:
        check: The gate to invoke.
        cwd:   Working tree the gate runs against (the ``origin/main`` worktree).
        env:   Base environment for the subprocess; ``check.env`` is layered on.

    Returns:
        A :class:`CheckResult`. Never raises — invocation problems become
        ``error`` (or ``skipped`` for optional checks).
    """
    started = monotonic()
    merged_env = {**env, **dict(check.env)}
    try:
        proc = subprocess.run(
            _expand_argv(check.argv, cwd),
            cwd=cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_HEALTH_GATE,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        elapsed = monotonic() - started
        status: CheckStatus = "skipped" if check.optional else "error"
        log.warning("health check %s could not run (%s): %s", check.check_id, status, exc)
        return CheckResult(check.check_id, status, f"invocation failed: {exc}", elapsed)

    elapsed = monotonic() - started
    output = _tail(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
    if proc.returncode == 0:
        return CheckResult(check.check_id, "pass", output, elapsed)
    if check.optional and "no such command" in (proc.stderr or "").lower():
        # Harness not wired yet (e.g. the behavioral eval): degrade, don't fail.
        return CheckResult(check.check_id, "skipped", output, elapsed)
    log.info("health check %s failed (exit %d)", check.check_id, proc.returncode)
    return CheckResult(check.check_id, "fail", output, elapsed)
