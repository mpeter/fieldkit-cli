"""CLI adapter for full-corpus skill routing evaluations."""

import json

import click

from fieldkit.commands.skill._runner import _load_all_skills
from fieldkit.errors import LLMError
from fieldkit.skill.routing import RoutingResult, SkillCandidate, judge_routing_case, load_routing_cases


def _load_candidates() -> tuple[SkillCandidate, ...]:
    """Adapt bundled skill metadata into the typed routing domain."""
    candidates: list[SkillCandidate] = []
    for raw_skill in _load_all_skills():
        name = raw_skill.get("name")
        description = raw_skill.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise LLMError("Bundled skill metadata has an invalid name or description", category="general")
        candidates.append(SkillCandidate(name=name, description=description))
    return tuple(candidates)


def _render_human(results: tuple[RoutingResult, ...]) -> None:
    click.echo("\nfieldkit Skill Eval — Routing")
    for result in results:
        if result.stub:
            icon = "⊘"
        elif result.passed:
            icon = "✓"
        else:
            icon = "✗"
        click.echo(f"  {icon} {result.case_id}: expected={result.expected_skill} selected={result.selected_skill}")
        click.echo(f"      {result.reason}")
    failed = sum(not result.passed for result in results)
    click.echo(f"\n  {len(results) - failed}/{len(results)} routing fixtures passing")


def run_routing_eval(*, json_output: bool, model: str | None) -> int:
    """Run every routing fixture against the complete bundled skill corpus."""
    candidates = _load_candidates()
    cases = load_routing_cases(candidates)
    results = tuple(judge_routing_case(case, candidates, model) for case in cases)
    failed = sum(not result.passed for result in results)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "mode": "routing",
                    "corpus_size": len(candidates),
                    "total": len(results),
                    "passed": len(results) - failed,
                    "failed": failed,
                    "results": [result.to_dict() for result in results],
                },
                indent=2,
            )
        )
    else:
        _render_human(results)
    return 1 if failed else 0
