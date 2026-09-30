"""Bind public product and design claims to their current code boundaries."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(_source(path))
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _calls(function: ast.FunctionDef) -> set[str]:
    return {
        node.func.id for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_product_model_root_claims_follow_config_accessors() -> None:
    concepts = _source("docs/concepts.md")
    paths = "src/fieldkit/config/_paths.py"
    assert "workspace is selected by `fieldkit_home`" in concepts
    assert "`FIELDKIT_DATA_DIR` when set, then the configured `fieldkit_data`" in concepts
    assert "`FIELDKIT_HARNESS_ROOT` overrides" in concepts
    assert "`$XDG_CACHE_HOME/fieldkit`" in concepts
    assert "fieldkit_home" in ast.unparse(_function(paths, "get_fieldkit_home"))
    assert {"get_fieldkit_home", "_resolve_absolute_root"} <= _calls(_function(paths, "_get_fieldkit_data_from_config"))
    assert "FIELDKIT_DATA_DIR" in ast.unparse(_function(paths, "get_fieldkit_data"))
    assert {"FIELDKIT_HARNESS_ROOT", "XDG_CACHE_HOME"} <= {
        node.value
        for node in ast.walk(_function(paths, "get_harness_scratch_root"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_local_report_claims_follow_command_domain_calls_and_output_names() -> None:
    concepts = _source("docs/concepts.md")
    design = _source("docs/design/local-first-cli.md")
    assert "`fieldkit pipeline --no-llm`" in concepts
    assert "`fieldkit brief generate --pipeline-only --no-llm`" in concepts
    assert "separate dated morning brief" in concepts
    assert "Pipeline collection and rendering belong to `fieldkit.pipeline`" in design
    assert "brief\n  collection and rendering belong to `fieldkit.brief`" in design

    pipeline_adapter = _function("src/fieldkit/commands/pipeline/cli.py", "_run")
    brief_adapter = _function("src/fieldkit/commands/brief/cli.py", "_run_pipeline_only")
    brief_command = _function("src/fieldkit/commands/brief/cli.py", "cmd_generate")
    assert "generate_review" in _calls(pipeline_adapter)
    assert "generate_pipeline_only" in _calls(brief_adapter)
    assert "_run_pipeline_only" in _calls(brief_command)
    assert "pipeline-review-" in ast.unparse(_function("src/fieldkit/pipeline/main.py", "generate_review"))
    assert "morning-brief-" in ast.unparse(_function("src/fieldkit/brief/pipeline_only.py", "generate_pipeline_only"))


def test_offline_first_success_claim_is_bounded_to_documented_steps() -> None:
    concepts = _source("docs/concepts.md")
    getting_started = _source("docs/getting-started.md")
    assert "minimal initialization,\nlocal diagnostics, and packaged skill discovery" in concepts
    assert "A workspace\nmust be initialized" in concepts
    for command in ("fieldkit init --minimal", "fieldkit doctor", "fieldkit skill list"):
        assert command in getting_started
    assert (
        "fieldkit brief generate --pipeline-only --no-llm"
        not in concepts.split("## Portable core and optional capabilities", maxsplit=1)[1].split(
            "## Reports from local work", maxsplit=1
        )[0]
    )
