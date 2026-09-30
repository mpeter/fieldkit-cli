"""Check AgentReady score gate.

Reads the assessment produced by the clean-worktree assessor and enforces:

1. Complete, bounded report evidence for the pinned tool and current clean HEAD.
2. Independently recomputed overall score >= 85.
3. Every frozen per-attribute floor, regardless of the aggregate result.

Exit codes:
    0 — all gates pass
    1 — failed score/floor, invalid or missing evidence, or dirty/stale candidate
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import TypeGuard

if __package__:
    from scripts import agentready_policy, git_worktree
    from scripts.json_policy import reject_duplicate_json_keys
else:
    import agentready_policy
    import git_worktree
    from json_policy import reject_duplicate_json_keys

# Module-level constants so tests can patch via monkeypatch.setattr.
JSON_PATH = Path(".agentready/assessment-latest.json")

_THRESHOLD = 85
_MAX_REPORT_BYTES = agentready_policy.MAX_REPORT_BYTES

# Frozen per-attribute floors. None leaves a known attribute informational.
# The pinned policy defines the full attribute set independently of these floors.
_FLOORS: dict[str, float | None] = {
    # Tier 1 — core health
    "test_execution": 100.0,
    "type_annotations": 100.0,
    "agent_instructions": 70.0,
    "ci_quality_gates": 90.0,
    "single_file_verification": 100.0,
    "readme_structure": 100.0,
    "standard_layout": 100.0,
    "lock_files": 100.0,
    "dependency_security": 30.0,
    # Tier 2 — quality
    "deterministic_enforcement": 35.0,
    "conventional_commits": 100.0,
    "gitignore_completeness": 85.0,
    "one_command_setup": 100.0,
    "file_size_limits": 42.7,
    "separation_of_concerns": 85.0,
    "inline_documentation": 75.0,
    "pattern_references": 35.0,
    "design_intent": 45.0,
    # Tier 3 — nice-to-have
    "architecture_decisions": 100.0,
    "adr_frontmatter_completeness": 100.0,
    "cyclomatic_complexity": 100.0,
    "structured_logging": None,
    "progressive_disclosure": 30.0,
    "architectural_boundaries": 100.0,
    "threat_model": 100.0,
    # Tier 4
    "issue_pr_templates": 100.0,
}


def _reject_constant(value: str) -> None:
    """Reject the nonstandard NaN and Infinity JSON extensions."""
    raise ValueError("nonfinite JSON constant")


def _finite_score(value: object) -> TypeGuard[int | float]:
    """Accept only real, finite percentage scores, excluding booleans."""
    return (
        type(value) in (int, float) and isinstance(value, (int, float)) and 0 <= value <= 100 and math.isfinite(value)
    )


def _current_head() -> str:
    """Require a clean candidate and resolve its committed identity."""
    repo = Path(__file__).resolve().parent.parent
    git_worktree.require_clean_worktree(repo)
    return git_worktree.head_revision(repo)


def _validate_evidence(data: dict[str, object]) -> None:
    """Require complete, unambiguous attribute evidence for this candidate."""
    if not _finite_score(data.get("overall_score")):
        raise ValueError("overall score must be a finite number from 0 to 100")
    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        raise ValueError("findings must be a nonempty list")
    seen: set[str] = set()
    scores: dict[str, float] = {}
    skipped = 0
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("attribute"), dict):
            raise ValueError("finding must contain an attribute object")
        attr_id = finding["attribute"].get("id")
        if not isinstance(attr_id, str) or re.fullmatch(r"[a-z][a-z0-9_]*", attr_id) is None:
            raise ValueError("invalid attribute id")
        if attr_id in seen:
            raise ValueError("duplicate attribute id")
        seen.add(attr_id)
        status = finding.get("status")
        score = finding.get("score")
        if (status == "not_applicable") != (attr_id in agentready_policy.NOT_APPLICABLE):
            raise ValueError("finding applicability does not match candidate policy")
        if status == "not_applicable":
            skipped += 1
            if score is not None or _FLOORS.get(attr_id) is not None:
                raise ValueError("tracked attributes must have applicable scores")
        elif status not in ("pass", "fail") or not _finite_score(score):
            raise ValueError("invalid finding status or score")
        else:
            scores[attr_id] = float(score)
    required = {name for name, floor in _FLOORS.items() if floor is not None}
    if required - seen:
        raise ValueError("missing required attribute evidence")
    if seen != set(agentready_policy.WEIGHTS):
        raise ValueError("findings do not match the pinned attribute set")
    counts = {
        "attributes_total": len(findings),
        "attributes_assessed": len(findings) - skipped,
        "attributes_skipped": skipped,
    }
    for name, expected in counts.items():
        actual = data.get(name)
        if type(actual) is not int or actual != expected:
            raise ValueError(f"invalid summary count: {name}")
    if data.get("schema_version") != agentready_policy.REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported report schema version")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("agentready_version") != agentready_policy.TOOL_VERSION:
        raise ValueError("assessment tool version does not match policy")
    if data["overall_score"] != agentready_policy.aggregate_score(scores):
        raise ValueError("reported aggregate does not match attribute scores")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("missing repository identity")
    revision = repository.get("commit_hash")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("invalid repository commit hash")
    if revision != _current_head():
        raise ValueError("assessment does not match the current candidate revision")


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
        with JSON_PATH.open("rb") as stream:
            payload = stream.read(_MAX_REPORT_BYTES + 1)
        if len(payload) > _MAX_REPORT_BYTES:
            raise ValueError("assessment exceeds the report byte limit")
        data = json.loads(
            payload,
            object_pairs_hook=reject_duplicate_json_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, ValueError, RecursionError) as exc:
        print(f"AgentReady: malformed JSON in {path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not isinstance(data, dict) or "overall_score" not in data:
        print(f"AgentReady: 'overall_score' field missing from {path}", file=sys.stderr)
        raise SystemExit(1)

    score_raw = data["overall_score"]
    if not isinstance(score_raw, (int, float)):
        print(
            f"AgentReady: 'overall_score' is not numeric (got {type(score_raw).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        _validate_evidence(data)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"AgentReady: invalid evidence: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    score: float = float(score_raw)

    tier_raw = str(data.get("certification_level") or data.get("tier", "Unknown"))[:64]
    tier = "".join(character if character.isprintable() else "?" for character in tier_raw)

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
            ratchet_failures.append(f"  {attr_id}: {attr_score!r} < floor {floor!r} — REGRESSION")

    if ratchet_failures:
        print("AgentReady floor ratchet FAIL:", file=sys.stderr)
        for line in ratchet_failures:
            print(line, file=sys.stderr)
        print(
            "Remediate the regressed attributes; frozen release floors remain unchanged.",
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
