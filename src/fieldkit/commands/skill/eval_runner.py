"""fieldkit skill eval — run static and behavioral assertions against skill definitions.

Static checks run automatically against the SKILL.md text (no LLM needed).
Behavioral evals use an LLM judge to grade per-assertion coverage (--behavioral).

Usage:
    fieldkit skill eval <skill-name>
    fieldkit skill eval --all
    fieldkit skill eval --failed        # show only skills with failing static checks
    fieldkit skill eval --json          # machine-readable output
    fieldkit skill eval --behavioral --all          # LLM-graded behavioral evals
    fieldkit skill eval --behavioral --skill NAME   # grade a single skill
    fieldkit skill eval --calibrate                 # validate judge accuracy
"""

import contextlib
import importlib.resources
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

import click

from fieldkit.commands.skill._runner import _skills_dir
from fieldkit.config import TIMEOUT_EVAL
from fieldkit.skill.judge import JudgeResponseError, SkillJudgement, judge_skill
from fieldkit.skill.template import build_template_ctx as _build_template_ctx
from fieldkit.skill.template import render_skill_text as _render_skill_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Skills excluded from behavioral corpus discovery (non-standard eval directory).
_BEHAVIORAL_SKIP: frozenset[str] = frozenset()


class _BehavioralJudgeError(TypedDict):
    skill_name: str
    category: Literal["general"]
    error: Literal["judge_failed"]


@dataclass(frozen=True)
class _BehavioralEvalRun:
    judgements: tuple[SkillJudgement, ...]
    errors: tuple[_BehavioralJudgeError, ...]
    without_evals: tuple[str, ...]
    not_covered_count: int
    unclear_count: int

    @property
    def attempted_count(self) -> int:
        return len(self.judgements) + len(self.errors) + len(self.without_evals)

    @property
    def with_evals_count(self) -> int:
        return len(self.judgements) + len(self.errors)


# ---------------------------------------------------------------------------
# Static check runners
# ---------------------------------------------------------------------------


def _check_contains(text: str, pattern: str) -> bool:
    return pattern in text


def _check_not_contains(text: str, pattern: str) -> bool:
    return pattern not in text


def _check_contains_all(text: str, patterns: list[str]) -> bool:
    return all(p in text for p in patterns)


def _check_commands_exist(commands: list[str]) -> tuple[bool, list[str]]:
    """Verify each command string is reachable via fieldkit --help output."""
    missing = []
    for cmd in commands:
        parts = cmd.split()
        # Run the parent group's --help and check the subcommand appears
        try:
            result = subprocess.run(
                [*parts[:-1], "--help"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_EVAL,
                check=False,
            )
            output = result.stdout + result.stderr
            # Check last part (subcommand) appears in help
            subcmd = parts[-1]
            if subcmd not in output:
                missing.append(cmd)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            missing.append(cmd)
    return len(missing) == 0, missing


def _run_static_check(check: dict[str, Any], skill_text: str) -> dict[str, Any]:
    """Run a single static check. Returns result dict."""
    check_id = check["id"]
    check_type = check.get("type", "contains")
    message = check.get("message", "")

    if check_type == "contains":
        passed = _check_contains(skill_text, check["pattern"])
        detail = f"Pattern not found: {check['pattern']!r}" if not passed else ""
    elif check_type == "not_contains":
        passed = _check_not_contains(skill_text, check["pattern"])
        detail = f"Forbidden pattern found: {check['pattern']!r}" if not passed else ""
    elif check_type == "contains_all":
        passed = _check_contains_all(skill_text, check["patterns"])
        missing = [p for p in check["patterns"] if p not in skill_text]
        detail = f"Missing patterns: {missing}" if not passed else ""
    elif check_type == "commands_exist":
        passed, missing_cmds = _check_commands_exist(check["commands"])
        detail = f"Commands not found in CLI: {missing_cmds}" if not passed else ""
    else:
        passed = False
        detail = f"Unknown check type: {check_type!r}"

    return {
        "id": check_id,
        "type": check_type,
        "passed": passed,
        "message": message,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Skill eval runner
# ---------------------------------------------------------------------------


def _load_skill_eval(skill_dir: Path) -> dict[str, Any] | None:
    """Load evals.json from a skill directory, resolving {{key}} templates."""
    evals_file = skill_dir / "evals" / "evals.json"
    if not evals_file.exists():
        return None
    try:
        raw = evals_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        ctx = _build_template_ctx()
    except OSError:  # config unavailable in test / offline context
        ctx = {}
    resolved, _unresolved = _render_skill_text(raw, ctx)
    try:
        parsed = json.loads(resolved)
    except json.JSONDecodeError:
        return None
    # Bare list: treat as {"evals": [...]} with no static_checks.
    if isinstance(parsed, list):
        return {"evals": parsed}
    if not isinstance(parsed, dict):
        return None
    return parsed


def _load_skill_text(skill_dir: Path) -> str:
    """Load all text content from a skill directory for pattern matching."""
    text_parts = []
    for md_file in skill_dir.rglob("*.md"):
        if "__pycache__" in str(md_file):
            continue
        with contextlib.suppress(OSError):
            text_parts.append(md_file.read_text(encoding="utf-8"))
    return "\n".join(text_parts)


def eval_skill(skill_dir: Path, *, verbose: bool = False, json_output: bool = False) -> dict[str, Any]:
    """Run all static checks for a skill. Returns structured result."""
    skill_name = skill_dir.name
    eval_data = _load_skill_eval(skill_dir)
    skill_text = _load_skill_text(skill_dir)

    result: dict[str, Any] = {
        "skill": skill_name,
        "has_evals": eval_data is not None,
        "static_results": [],
        "behavioral_cases": [],
        "static_pass": 0,
        "static_fail": 0,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }

    if eval_data is None:
        result["no_evals_message"] = "No evals.json found — create evals/evals.json to enable static checks"
        return result

    # Run static checks
    for check in eval_data.get("static_checks", []):
        check_result = _run_static_check(check, skill_text)
        result["static_results"].append(check_result)
        if check_result["passed"]:
            result["static_pass"] += 1
        else:
            result["static_fail"] += 1

    # Collect behavioral cases (for human review)
    result["behavioral_cases"] = eval_data.get("evals", [])

    return result


def _render_eval_result(result: dict[str, Any], *, show_behavioral: bool = True) -> None:
    """Print human-readable eval result to stdout."""
    skill = result["skill"]
    has_evals = result["has_evals"]

    click.echo(f"\n{'─' * 60}")
    click.echo(f"  {skill}")
    click.echo(f"{'─' * 60}")

    if not has_evals:
        click.echo(f"  ⚠  No evals.json — {result.get('no_evals_message', 'create evals to enable checking')}")
        return

    # Static checks
    total = result["static_pass"] + result["static_fail"]
    if total == 0:
        click.echo("  i  No static checks defined")
    else:
        status = "✅" if result["static_fail"] == 0 else "❌"
        click.echo(f"  {status} Static checks: {result['static_pass']}/{total} passing")
        for r in result["static_results"]:
            icon = "  ✓" if r["passed"] else "  ✗"
            msg = r["message"] or r["id"]
            click.echo(f"{icon}  {msg}")
            if not r["passed"] and r["detail"]:
                click.echo(f"       → {r['detail']}")

    # Behavioral cases
    cases = result["behavioral_cases"]
    if cases and show_behavioral:
        click.echo(f"\n  📋 Behavioral evals ({len(cases)} cases — human review needed):")
        for case in cases:
            click.echo(f'\n  [{case["id"]}] Prompt: "{case["prompt"]}"')
            expected = case.get("expected_behavior") or case.get("prompt") or "(no description)"
            click.echo(f"       Expected: {expected}")
            click.echo("       Assertions:")
            for assertion in case.get("assertions", []):
                # Corpus stores assertions as plain strings; legacy scaffolds used {"text": ...}.
                text = assertion["text"] if isinstance(assertion, dict) else assertion
                click.echo(f"         □ {text}")


# ---------------------------------------------------------------------------
# Behavioral eval corpus discovery (T12)
# ---------------------------------------------------------------------------


def _resolve_behavioral_skill_dirs(
    skills_dir: Path,
    run_all: bool,
    skill_names: list[str],
    limit: int | None,
) -> list[Path] | None:
    """Resolve skill directories for behavioral evaluation.

    Filters out skills in _BEHAVIORAL_SKIP and skills without evals/evals.json.
    Applies --limit after resolution. Returns None on error (already printed).

    Args:
        skills_dir: Root skills directory.
        run_all: If True, enumerate all eligible skills.
        skill_names: Explicit skill names to evaluate (from --skill flags).
        limit: Maximum number of skill dirs to return; None means no limit.

    Returns:
        Sorted list of skill directories, or None if an error occurred.
    """
    if run_all:
        # Enumerate all dirs with evals/evals.json, excluding _BEHAVIORAL_SKIP.
        candidates = sorted(
            d
            for d in skills_dir.iterdir()
            if d.is_dir()
            and not d.name.startswith("_")
            and d.name not in _BEHAVIORAL_SKIP
            and (d / "evals" / "evals.json").exists()
        )
        if not candidates:
            # Guard against false-green: fail loudly if no skills qualify.
            # This catches misconfigured FIELDKIT_SKILLS_DIR and fresh clones
            # where skills are not yet symlinked.
            click.echo(
                "No skills with evals/evals.json found — check FIELDKIT_SKILLS_DIR",
                err=True,
            )
            return None
        if limit is not None:
            candidates = candidates[:limit]
        return candidates

    if skill_names:
        # Single iterdir() scan to avoid O(n) repeated directory reads per name.
        all_dirs: dict[str, Path] = {d.name: d for d in skills_dir.iterdir() if d.is_dir()}
        skill_dirs: list[Path] = []
        for name in skill_names:
            if name in _BEHAVIORAL_SKIP:
                click.echo(f"Skill {name!r} is excluded from behavioral evals (in _BEHAVIORAL_SKIP)", err=True)
                return None
            if name not in all_dirs:
                click.echo(f"Skill not found: {name!r}", err=True)
                return None
            skill_dirs.append(all_dirs[name])
        if limit is not None:
            skill_dirs = skill_dirs[:limit]
        return skill_dirs

    click.echo("Usage: fieldkit skill eval --behavioral (--all | --skill NAME)", err=True)
    return None


# ---------------------------------------------------------------------------
# Behavioral eval rendering (T17)
# ---------------------------------------------------------------------------


def _render_judgement(judgement: SkillJudgement) -> None:
    """Print a per-skill verdict table to stdout.

    Displays the skill name as a section header, then a row per assertion
    showing the verdict icon and reason. Stub verdicts are rendered with a
    distinct icon to make NO_LLM=1 runs visually identifiable.

    Args:
        judgement: The SkillJudgement returned by judge_skill().
    """
    click.echo(f"\n{'─' * 60}")
    click.echo(f"  {judgement.skill_name}  [behavioral]")
    click.echo(f"{'─' * 60}")

    if judgement.stub:
        click.echo("  ⊘  Stub mode (NO_LLM=1) — all verdicts are 'stub'")

    for case in judgement.cases:
        click.echo(f"\n  Case {case.case_id}:")
        for v in case.verdicts:
            if v.verdict == "covered":
                icon = "  ✓"
            elif v.verdict == "not-covered":
                icon = "  ✗"
            elif v.verdict == "unclear":
                icon = "  ?"
            else:  # stub
                icon = "  ⊘"
            click.echo(f"{icon}  [{v.verdict}] assertion {v.assertion_idx}: {v.reason}")


# ---------------------------------------------------------------------------
# Behavioral eval runner (T14)
# ---------------------------------------------------------------------------


def _run_behavioral_evals(
    skill_dirs: list[Path],
    model: str | None,
    *,
    json_output: bool,
) -> _BehavioralEvalRun:
    """Run LLM-graded behavioral evals for the given skill directories.

    Calls judge_skill() once per skill, collects results, and renders per-skill
    verdict tables (unless --json). General per-skill judge errors are recorded
    and the loop continues; provider errors propagate to cli_main().

    The --limit is applied upstream in _resolve_behavioral_skill_dirs() before this
    function is called — do not re-apply it here.

    Args:
        skill_dirs: Skill directories to evaluate (already limited by caller).
        model: Optional LiteLLM model override; None uses the default chain.
        json_output: When True, suppress per-skill rendering (output is in summary).

    Returns:
        The complete behavioral run, including partial errors and skipped skills.
    """
    judgements: list[SkillJudgement] = []
    judge_errors: list[_BehavioralJudgeError] = []
    without_evals: list[str] = []
    not_covered_count = 0
    unclear_count = 0

    if not json_output:
        click.echo(f"\nfieldkit Skill Eval — Behavioral — {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')} UTC")
        click.echo(f"Judging {len(skill_dirs)} skill(s)...")

    for skill_dir in skill_dirs:
        skill_name = skill_dir.name
        skill_text = _load_skill_text(skill_dir)
        eval_data = _load_skill_eval(skill_dir)
        raw_cases: object = eval_data.get("evals", []) if eval_data else []

        if not isinstance(raw_cases, list) or not raw_cases or not all(isinstance(case, dict) for case in raw_cases):
            without_evals.append(skill_name)
            click.echo(f"  [without evals] {skill_name}: no behavioral cases", err=True)
            continue
        cases: list[dict[str, Any]] = raw_cases

        try:
            judgement = judge_skill(skill_name, skill_text, cases, model)
        except JudgeResponseError:
            judge_errors.append({"skill_name": skill_name, "category": "general", "error": "judge_failed"})
            click.echo(f"  [judge error] {skill_name}: judge_failed (general)", err=True)
            continue
        judgements.append(judgement)

        # Accumulate verdict counts for exit-code logic (D8).
        for case in judgement.cases:
            for v in case.verdicts:
                if v.verdict == "not-covered":
                    not_covered_count += 1
                elif v.verdict == "unclear":
                    unclear_count += 1

        if not json_output:
            _render_judgement(judgement)

    return _BehavioralEvalRun(
        judgements=tuple(judgements),
        errors=tuple(judge_errors),
        without_evals=tuple(without_evals),
        not_covered_count=not_covered_count,
        unclear_count=unclear_count,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _scaffold_evals(skill_dir: Path) -> int:
    """Create a starter evals.json for a skill that has none."""
    evals_dir = skill_dir / "evals"
    evals_file = evals_dir / "evals.json"
    if evals_file.exists():
        # implementation change: exit 1 when evals.json already exists (prevents accidental overwrite)
        click.echo(f"Error: evals.json already exists: {evals_file}", err=True)
        return 1
    evals_dir.mkdir(exist_ok=True)
    skill_name = skill_dir.name
    scaffold = {
        "evals": [
            {
                "id": f"{skill_name}-basic",
                "prompt": f"[TODO: describe a prompt that should trigger {skill_name}]",
                "expected_behavior": "[TODO: what the agent should do]",
                "assertions": ["[TODO: what to verify]"],
            }
        ]
    }
    evals_file.write_text(json.dumps(scaffold, indent=2) + "\n", encoding="utf-8")
    click.echo(f"Created: {evals_file}")
    return 0


def _resolve_skill_dirs(
    skills_dir: Path,
    run_all: bool,
    skill_names: list[str],
) -> list[Path] | None:
    """Return skill directories to evaluate, or None on error (already printed)."""
    if run_all:
        return sorted(d for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith("_"))
    if skill_names:
        skill_dirs: list[Path] = []
        for name in skill_names:
            candidates = [d for d in skills_dir.iterdir() if d.is_dir() and d.name == name]
            if not candidates:
                click.echo(f"Skill not found: {name!r}", err=True)
                return None
            skill_dirs.extend(candidates)
        return skill_dirs
    click.echo("Usage: fieldkit skill eval <name> | --all | --failed", err=True)
    click.echo("       --json          machine-readable output", err=True)
    click.echo("       --no-behavioral skip behavioral eval listing", err=True)
    return None


def _run_evals(
    skill_dirs: list[Path],
    *,
    failed_only: bool,
    json_output: bool,
    no_behavioral: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Run evals over skill_dirs and return (all_results, overall_failures)."""
    if not json_output:
        click.echo(f"\nfieldkit Skill Eval — {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')} UTC")
        click.echo(f"Evaluating {len(skill_dirs)} skill(s)...")

    all_results: list[dict[str, Any]] = []
    overall_failures = 0
    for skill_dir in skill_dirs:
        result = eval_skill(skill_dir, verbose=not json_output)
        all_results.append(result)
        overall_failures += result["static_fail"]
        # --failed: only render skills with actual static check failures.
        # Skills with no evals have static_fail == 0 and are skipped too.
        if failed_only and result["static_fail"] == 0:
            continue
        if not json_output:
            _render_eval_result(result, show_behavioral=not no_behavioral and not (len(skill_dirs) > 1))
    return all_results, overall_failures


def _compute_static_summary(
    all_results: list[dict[str, Any]],
    overall_failures: int,
) -> tuple[int, int, int, int]:
    """Compute aggregate counts from static eval results.

    Returns:
        (skills_with_evals, skills_without_evals, total_static_pass, total_static)
    """
    skills_with_evals = sum(1 for r in all_results if r["has_evals"])
    skills_without_evals = sum(1 for r in all_results if not r["has_evals"])
    total_static_pass = sum(r["static_pass"] for r in all_results)
    total_static = total_static_pass + overall_failures
    return skills_with_evals, skills_without_evals, total_static_pass, total_static


def _print_json_summary(
    all_results: list[dict[str, Any]],
    overall_failures: int,
    *,
    total_evaluated: int,
    behavioral_run: _BehavioralEvalRun | None,
) -> None:
    """Emit the evaluation summary as a JSON object to stdout."""
    skills_with_evals, skills_without_evals, total_static_pass, _ = _compute_static_summary(
        all_results, overall_failures
    )

    summary: dict[str, Any] = {
        "evaluated": total_evaluated,
        "with_evals": skills_with_evals + (behavioral_run.with_evals_count if behavioral_run else 0),
        "without_evals": skills_without_evals + (len(behavioral_run.without_evals) if behavioral_run else 0),
        "static_pass": total_static_pass,
        "static_fail": overall_failures,
        "results": all_results,
    }
    if behavioral_run is not None:
        summary["behavioral_results"] = [judgement.to_dict() for judgement in behavioral_run.judgements]
        summary["behavioral_not_covered"] = behavioral_run.not_covered_count
        summary["behavioral_unclear"] = behavioral_run.unclear_count
        if behavioral_run.errors:
            summary["behavioral_errors"] = behavioral_run.errors
        if behavioral_run.without_evals:
            summary["behavioral_without_evals"] = behavioral_run.without_evals
    click.echo(json.dumps(summary, indent=2))


def _print_human_summary(
    all_results: list[dict[str, Any]],
    overall_failures: int,
    *,
    total_evaluated: int,
    behavioral_run: _BehavioralEvalRun | None,
) -> None:
    """Emit the evaluation summary in human-readable format to stdout."""
    skills_with_evals, skills_without_evals, total_static_pass, total_static = _compute_static_summary(
        all_results, overall_failures
    )

    click.echo(f"\n{'═' * 60}")
    click.echo("SUMMARY")
    click.echo(f"{'═' * 60}")
    click.echo(f"  Skills evaluated:  {total_evaluated}")

    if behavioral_run is not None and len(all_results) == 0:
        _print_behavioral_section(
            not_covered_count=behavioral_run.not_covered_count,
            behavioral_judgements=behavioral_run.judgements,
        )
    else:
        _print_static_section(
            skills_with_evals=skills_with_evals,
            skills_without_evals=skills_without_evals,
            total_static=total_static,
            total_static_pass=total_static_pass,
            overall_failures=overall_failures,
        )

    if behavioral_run and behavioral_run.errors:
        click.echo(f"\n  ❌ {len(behavioral_run.errors)} skill(s) failed judge", err=True)
    if behavioral_run and behavioral_run.without_evals:
        click.echo(f"\n  ⚠  {len(behavioral_run.without_evals)} skill(s) without behavioral evals", err=True)
    if behavioral_run and behavioral_run.unclear_count > 0:
        click.echo(
            f"\n  ⚠  {behavioral_run.unclear_count} unclear verdict(s) in behavioral evals "
            "(advisory — skill coverage is ambiguous; review and clarify assertions)",
            err=True,
        )


def _print_behavioral_section(
    *,
    not_covered_count: int,
    behavioral_judgements: tuple[SkillJudgement, ...],
) -> None:
    """Print behavioral-only summary lines."""
    stub_count = sum(1 for judgement in behavioral_judgements if judgement.stub)
    live_count = len(behavioral_judgements) - stub_count
    if behavioral_judgements and stub_count == len(behavioral_judgements):
        click.echo(f"  Behavioral (stub): {stub_count} skills (NO_LLM=1 — no API calls)")
    else:
        click.echo(f"  Behavioral (live): {live_count} skills judged  |  Stub: {stub_count}")
        if not_covered_count > 0:
            click.echo(f"  Not covered:       {not_covered_count} assertion(s) — fix skill or eval")


def _print_static_section(
    *,
    skills_with_evals: int,
    skills_without_evals: int,
    total_static: int,
    total_static_pass: int,
    overall_failures: int,
) -> None:
    """Print static-eval summary lines."""
    click.echo(f"  Skills with evals: {skills_with_evals}  |  Without: {skills_without_evals}")
    if total_static > 0:
        click.echo(f"  Static checks:     {total_static_pass}/{total_static} passing")
    if overall_failures == 0 and skills_with_evals > 0:
        click.echo("\n  ✅ All static checks passing")
    elif overall_failures > 0:
        click.echo(f"\n  ❌ {overall_failures} static check(s) failing")


def _print_eval_summary(
    all_results: list[dict[str, Any]],
    overall_failures: int,
    *,
    json_output: bool,
    behavioral_run: _BehavioralEvalRun | None = None,
) -> None:
    """Print summary to stdout in either JSON or human format.

    Args:
        all_results: Static eval results for all evaluated skills.
        overall_failures: Total static check failures across all skills.
        json_output: When True, emit a single JSON object to stdout.
        behavioral_run: Complete result from behavioral evals, when requested.
    """
    total_evaluated = len(all_results) + (behavioral_run.attempted_count if behavioral_run else 0)

    if json_output:
        _print_json_summary(
            all_results,
            overall_failures,
            total_evaluated=total_evaluated,
            behavioral_run=behavioral_run,
        )
        return

    _print_human_summary(
        all_results,
        overall_failures,
        total_evaluated=total_evaluated,
        behavioral_run=behavioral_run,
    )


def _load_calibration_fixtures() -> list[dict[str, Any]]:
    """Load calibration fixtures from the package data file.

    Uses importlib.resources per design.md Risks §1 — never __file__-relative
    paths or get_fieldkit_home() for package data assets.

    Returns:
        List of fixture dicts with id, skill_excerpt, prompt, assertion,
        gold_verdict, and gold_reason keys.

    Raises:
        OSError: If the package data file cannot be read.
        json.JSONDecodeError: If the file is malformed.
    """
    data_ref = importlib.resources.files("fieldkit._data").joinpath("skill-eval-calibration.json")
    with importlib.resources.as_file(data_ref) as p:
        raw: list[dict[str, Any]] = json.loads(p.read_text(encoding="utf-8"))
        return raw


def _run_calibrate(json_output: bool = False, model: str | None = None) -> int:
    """Run calibration fixtures to validate judge accuracy.

    Loads the 6 hand-built calibration fixtures from package data and runs each
    through judge_skill(). Compares the first assertion verdict to the fixture's
    gold_verdict. Exits 0 iff all 6 match; exits 1 on any mismatch.

    Fixture-to-judge_skill() mapping (design.md Risks §1):
        skill_name  = fixture["id"]
        skill_text  = fixture["skill_excerpt"]
        cases       = [{"id": 0, "prompt": fixture["prompt"],
                        "expected_behavior": "", "assertions": [fixture["assertion"]]}]

    The expected_behavior field is intentionally empty — the judge grades only
    the assertions, not the human-written answer key (D2).

    Args:
        json_output: If True, emit calibration results as JSON to stdout.
        model: Optional LiteLLM model override; None uses the default chain.

    Returns:
        0 if all 6 gold verdicts match (or all stub); 1 on partial match or load error.
    """
    try:
        fixtures = _load_calibration_fixtures()
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Error: cannot load calibration fixtures: {exc}", err=True)
        return 1

    results: list[dict[str, Any]] = []
    pass_count = 0

    for fixture in fixtures:
        fixture_id: str = fixture["id"]
        cases = [
            {
                "id": 0,
                "prompt": fixture["prompt"],
                "expected_behavior": "",  # intentionally empty — must not bias the judge (D2)
                "assertions": [fixture["assertion"]],
            }
        ]

        judgement = judge_skill(
            skill_name=fixture_id,
            skill_text=fixture["skill_excerpt"],
            cases=cases,
            model=model,
        )

        # Extract the verdict for the single assertion in case 0.
        # Stub mode produces verdict="stub" which is treated as a pass (D5, D8).
        if judgement.cases:
            first_case = judgement.cases[0]
            actual_verdict = first_case.verdicts[0].verdict if first_case.verdicts else "stub"
            reason = first_case.verdicts[0].reason if first_case.verdicts else "stub"
        else:
            actual_verdict = "stub"
            reason = "no cases returned"

        gold_verdict: str = fixture["gold_verdict"]
        # Stub verdicts count as pass (D8: "all stub → exit 0").
        passed = actual_verdict == gold_verdict or actual_verdict == "stub"
        if passed:
            pass_count += 1

        result_entry: dict[str, Any] = {
            "id": fixture_id,
            "gold_verdict": gold_verdict,
            "actual_verdict": actual_verdict,
            "passed": passed,
            "reason": reason,
            "stub": judgement.stub,
        }
        results.append(result_entry)

        if not json_output:
            icon = "✅" if passed else "❌"
            stub_tag = " [stub]" if judgement.stub else ""
            click.echo(f"  {icon}  {fixture_id}{stub_tag}")
            click.echo(f"       gold={gold_verdict!r}  actual={actual_verdict!r}")
            if not passed:
                click.echo(f"       reason: {reason}")

    total = len(fixtures)
    all_passed = pass_count == total

    if json_output:
        click.echo(
            json.dumps(
                {
                    "calibration": True,
                    "total": total,
                    "passed": pass_count,
                    "failed": total - pass_count,
                    "all_passed": all_passed,
                    "results": results,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"\n{'═' * 60}")
        if all_passed:
            click.echo(f"  ✅ Calibration passed: {pass_count}/{total} gold verdicts match")
        else:
            click.echo(f"  ❌ Calibration failed: {pass_count}/{total} gold verdicts match")
            click.echo("     Judge needs recalibration — check model or prompt changes.")
        click.echo(f"{'═' * 60}")

    # D8: exit 0 iff all 6 gold verdicts match; exit 1 on partial match.
    return 0 if all_passed else 1


def run_eval_cmd(
    *,
    skill_names: list[str],
    run_all: bool = False,
    failed_only: bool = False,
    json_output: bool = False,
    no_behavioral: bool = False,
    scaffold_name: str | None = None,
    behavioral: bool = False,
    calibrate: bool = False,
    limit: int | None = None,
    model: str | None = None,
) -> int:
    """Entry point for 'fieldkit skill eval' subcommand.

    Takes the values Click has already parsed. Callers MUST NOT re-serialize them
    into an argv list — that round trip is what this signature exists to remove.

    Args:
        skill_names: Skills to evaluate. `--skill` values first, then bare
            positional names; order is significant because `limit` truncates.
        run_all: Evaluate every skill (`--all`).
        failed_only: Show only skills with failing checks (`--failed`).
        json_output: Emit machine-readable JSON (`--json`).
        no_behavioral: Skip printing behavioral eval cases (`--no-behavioral`).
        scaffold_name: Skill to scaffold an evals.json for (`--scaffold`). An
            empty string is a usage error; None means the flag was absent.
        behavioral: Run LLM-graded behavioral evals (`--behavioral`).
        calibrate: Run calibration fixtures (`--calibrate`).
        limit: Max skills to judge in behavioral mode (`--limit`).
        model: Optional LiteLLM model override for behavioral evals.

    Returns:
        Exit code: 0 on success, 1 on partial failures; provider exceptions
        propagate for canonical exit mapping.
    """
    if scaffold_name == "":
        click.echo("Usage: fieldkit skill eval --scaffold <skill-name>", err=True)
        return 1

    skills_dir = _skills_dir()
    if not skills_dir.is_dir():
        click.echo(f"Skills directory not found: {skills_dir}", err=True)
        return 1

    # --calibrate and --behavioral are mutually exclusive (D6).
    if calibrate and behavioral:
        click.echo("Error: --calibrate and --behavioral are mutually exclusive", err=True)
        return 1

    if scaffold_name is not None:
        candidates = [d for d in skills_dir.iterdir() if d.is_dir() and d.name == scaffold_name]
        if not candidates:
            click.echo(f"Skill not found: {scaffold_name!r}", err=True)
            return 1
        return _scaffold_evals(candidates[0])

    # --calibrate sub-path: T10 (Phase 2) — validate judge accuracy against gold fixtures.
    if calibrate:
        return _run_calibrate(json_output=json_output, model=model)

    # --behavioral sub-path: LLM-graded behavioral evals (T14-T17).
    if behavioral:
        skill_dirs = _resolve_behavioral_skill_dirs(skills_dir, run_all, skill_names, limit)
        if skill_dirs is None:
            return 1

        behavioral_run = _run_behavioral_evals(
            skill_dirs,
            model,
            json_output=json_output,
        )

        # T16/T17: Print summary with behavioral results.
        # Pass empty static results when running behavioral-only.
        _print_eval_summary(
            [],
            0,
            json_output=json_output,
            behavioral_run=behavioral_run,
        )

        # T15: Exit-code logic (D8).
        # - Any not-covered → 1 (partial failure).
        # - Any contained judge error → 1 (partial failure).
        # - Any unclear, with no partial condition → 0 (advisory warning already printed).
        # - All covered or stub → 0.
        # LLMError(auth/rate-limit) propagates to cli_main() (not hand-rolled).
        return 1 if behavioral_run.not_covered_count > 0 or behavioral_run.errors else 0

    # Static eval path (original behaviour).
    skill_dirs_static = _resolve_skill_dirs(skills_dir, run_all, skill_names)
    if skill_dirs_static is None:
        return 1

    all_results, overall_failures = _run_evals(
        skill_dirs_static,
        failed_only=failed_only,
        json_output=json_output,
        no_behavioral=no_behavioral,
    )
    _print_eval_summary(all_results, overall_failures, json_output=json_output)
    return 1 if overall_failures > 0 else 0
