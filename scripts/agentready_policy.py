"""Pinned AgentReady report and scoring contract for the fieldkit configuration.

Weights follow AgentReady 2.49.0's data/default-weights.yaml and the dbt
assessors' fallback weights. The configured openapi_specs exclusion is omitted.
The aggregate normalizes over applicable findings and rounds to one decimal,
matching services/scorer.py; report-supplied weights are never authoritative.
"""

TOOL_VERSION = "2.49.0"
PACKAGE_SPEC = f"agentready=={TOOL_VERSION}"
REPORT_SCHEMA_VERSION = "1.0.0"
MAX_REPORT_BYTES = 2 * 1024 * 1024
NOT_APPLICABLE = frozenset(
    {"dbt_project_config", "dbt_model_documentation", "dbt_data_tests", "dbt_project_structure", "container_setup"}
)

WEIGHTS: dict[str, float] = {
    "test_execution": 0.11,
    "type_annotations": 0.10,
    "agent_instructions": 0.07,
    "ci_quality_gates": 0.05,
    "single_file_verification": 0.05,
    "readme_structure": 0.05,
    "standard_layout": 0.05,
    "lock_files": 0.05,
    "dependency_security": 0.05,
    "dbt_project_config": 0.10,
    "dbt_model_documentation": 0.10,
    "deterministic_enforcement": 0.03,
    "conventional_commits": 0.03,
    "gitignore_completeness": 0.03,
    "one_command_setup": 0.03,
    "file_size_limits": 0.03,
    "separation_of_concerns": 0.03,
    "inline_documentation": 0.03,
    "pattern_references": 0.03,
    "design_intent": 0.03,
    "dbt_data_tests": 0.03,
    "dbt_project_structure": 0.03,
    "architecture_decisions": 0.01,
    "adr_frontmatter_completeness": 0.02,
    "cyclomatic_complexity": 0.02,
    "structured_logging": 0.01,
    "progressive_disclosure": 0.01,
    "architectural_boundaries": 0.02,
    "threat_model": 0.02,
    "issue_pr_templates": 0.01,
    "container_setup": 0.01,
}


def aggregate_score(scores: dict[str, float]) -> float:
    """Recompute the pinned aggregate from validated applicable scores."""
    total_weight = sum(WEIGHTS[name] for name in scores)
    if not total_weight:
        raise ValueError("assessment has no applicable weighted findings")
    return round(sum(score * WEIGHTS[name] for name, score in scores.items()) / total_weight, 1)
