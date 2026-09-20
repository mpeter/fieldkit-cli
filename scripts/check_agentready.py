"""Check AgentReady score gate.

Reads the assessment JSON written by ``uvx agentready assess .`` and enforces:

1. Overall score >= threshold (currently 85).
2. Per-attribute floor ratchet — no individual attribute score may drop below
   its recorded floor without an explicit update to this file.

The floor ratchet catches silent regressions in attributes that currently pass
the overall gate but could quietly deteriorate. To lower a floor (intentional
regression), update the ``_FLOORS`` dict and add a comment explaining why.

Exit codes:
    0 — all gates pass
    1 — score < threshold, floor regression, file missing, malformed JSON,
        or missing field
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Module-level constants so tests can patch via monkeypatch.setattr.
JSON_PATH = Path(".agentready/assessment-latest.json")

_THRESHOLD = 85

# Per-attribute score floors.  None = not tracked (new/not_applicable).
# Ratchet rule: lower a floor only with an explicit comment.
# To raise a floor after improvement: update the value and commit.
# Attributes not listed here are untracked (new attributes auto-discover).
_FLOORS: dict[str, float | None] = {
    # Tier 1 — core health
    "test_execution": 100.0,
    "type_annotations": 100.0,
    "agent_instructions": 70.0,  # AGENTS.md is 304 lines (exceeds 150-line limit); score=70 stable.
    # Root cause: AGENTS.md has grown with project complexity. Splitting into per-domain
    # skills is tracked separately. Floor set to measured value (100 was aspirational).
    "ci_quality_gates": 90.0,  # was 95; allow minor CI config drift
    "single_file_verification": 100.0,
    "readme_structure": 100.0,
    "standard_layout": 100.0,
    "lock_files": 100.0,
    "dependency_security": 30.0,  # 35 today; intentionally low (Dependabot-only)
    # Tier 2 — quality
    "deterministic_enforcement": 35.0,
    "conventional_commits": 100.0,
    "gitignore_completeness": 85.0,
    "one_command_setup": 100.0,
    "file_size_limits": 42.7,  # Measured 42.89586305278174 after the module split
    # reduced check_skill_integrity.py from 1,309 lines to an 87-line adapter with
    # cohesive modules capped below 400 lines. Remaining large files are tracked separately.
    "separation_of_concerns": 85.0,
    "inline_documentation": 75.0,  # 80.6 today; floor below to allow fluctuation
    "pattern_references": 35.0,  # 40 now; floor below actual to allow minor drift.
    # Root cause of prior 0-score: assessor regexes only match backtick paths
    # (see `x` for), not markdown link syntax ([x](path)). AGENTS.md:139 now
    # uses backtick form — score is stable at 40.
    "design_intent": 45.0,  # 50 today
    # Tier 3 — nice-to-have
    "architecture_decisions": 100.0,
    "adr_frontmatter_completeness": 100.0,
    "cyclomatic_complexity": 100.0,
    "structured_logging": None,  # FAIL (0) — excluded from tool chain; not tracked
    "progressive_disclosure": 30.0,  # score=30 stable; 7 subdirectory AGENTS.md files present.
    # Path-scoped .claude/rules/ not yet configured. Floor set to measured value.
    "architectural_boundaries": 100.0,
    "threat_model": 100.0,
    # Tier 4
    "issue_pr_templates": 100.0,
}


def main() -> None:
    """Read the AgentReady assessment JSON and enforce score gates."""
    path = JSON_PATH.resolve()

    if not JSON_PATH.exists():
        print(
            f"AgentReady assessment not found: {path}\nRun 'uvx agentready assess .' from the repo root first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"AgentReady: malformed JSON in {path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if "overall_score" not in data:
        print(f"AgentReady: 'overall_score' field missing from {path}", file=sys.stderr)
        raise SystemExit(1)

    score_raw = data["overall_score"]
    if not isinstance(score_raw, (int, float)):
        print(
            f"AgentReady: 'overall_score' is not numeric (got {type(score_raw).__name__}): {score_raw!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    score: float = float(score_raw)

    # Cap at 64 chars to prevent log injection via a crafted assessment file.
    tier: str = str(data.get("certification_level") or data.get("tier", "Unknown"))[:64]

    # --- Overall gate ---
    overall_ok = score >= _THRESHOLD
    if overall_ok:
        print(f"AgentReady overall: {score:.1f}/100 ({tier}) ✓")
    else:
        print(f"AgentReady overall: {score:.1f}/100 ({tier}) — FAIL (threshold: {_THRESHOLD})")

    # --- Per-attribute floor ratchet ---
    findings = data.get("findings", [])
    ratchet_failures: list[str] = []
    untracked_fails: list[str] = []

    for finding in findings:
        attr_id: str = finding.get("attribute", {}).get("id", "")
        if not attr_id:
            continue

        raw_score = finding.get("score")
        status = finding.get("status", "")

        # not_applicable → skip
        if status == "not_applicable" or raw_score is None:
            continue

        attr_score = float(raw_score)
        floor = _FLOORS.get(attr_id)

        if floor is None:
            # Attribute intentionally untracked or new
            if status == "fail":
                untracked_fails.append(f"  {attr_id}: {attr_score:.0f} (FAIL — untracked, add to _FLOORS)")
            continue

        if attr_score < floor:
            ratchet_failures.append(f"  {attr_id}: {attr_score:.1f} < floor {floor:.1f} — REGRESSION")

    if ratchet_failures:
        print("AgentReady floor ratchet FAIL:", file=sys.stderr)
        for line in ratchet_failures:
            print(line, file=sys.stderr)
        print(
            "To accept a regression: lower the floor in scripts/check_agentready.py with a comment.",
            file=sys.stderr,
        )

    if untracked_fails:
        print("AgentReady untracked FAILs (informational — add to _FLOORS to gate):")
        for line in untracked_fails:
            print(line)

    if not overall_ok or ratchet_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
