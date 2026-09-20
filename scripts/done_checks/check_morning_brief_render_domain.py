"""Verify implementation change step 2 symbol ownership and removal of stale import paths."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PURSUIT_SYMBOLS = {
    "COMPLETED_STAGES",
    "ProjectRow",
    "_extract_project_name",
    "_parse_iso_date",
    "classify_project",
    "health_check",
}
WATCH_SYMBOLS = {
    "_render_companion_outbox_pointer",
    "_render_project_health_section",
    "_render_quota_section",
    "render_brief",
}
OLD_RENDER_MODULE = "fieldkit.commands.watch.morning_brief_render"
OLD_RENDER_DOC = "commands/watch/morning_brief_render.py"
OLD_CLASSIFIER_IMPORT = "fieldkit.commands.pursuit.projects_health import classify_project"
OLD_CLASSIFIER_PATCH = "fieldkit.commands.pursuit.projects_health.classify_project"
OLD_PARSER_GUIDANCE = "import `_parse_frontmatter` from `audit.py`"
COMPANION_TESTS = {
    "test_companion_pointer_missing_outbox_is_empty",
    "test_companion_pointer_empty_outbox_is_empty",
    "test_companion_pointer_unreadable_outbox_is_empty",
    "test_companion_pointer_populated_outbox_has_exact_output",
    "test_render_brief_places_companion_pointer_before_footer",
}
CLASSIFIER_TEST = "test_classify_project_empty_frontmatter_is_unknown"
QUOTA_RENDER_TESTS = {
    "test_quota_section_passes_collector_result_identity_to_calculator",
    "test_quota_section_without_config_does_not_collect",
}
QUOTA_ORCHESTRATION_TEST = "test_run_generate_inner_passes_quota_collector_identity_to_render_brief"


def _definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols = {
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            symbols.update(target.id for target in targets if isinstance(target, ast.Name))
    return symbols


def _command_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if (
            (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("fieldkit.commands"))
            or (
                isinstance(node, ast.Import) and any(alias.name.startswith("fieldkit.commands") for alias in node.names)
            )
        )
    ]


def _imports_from(path: Path, module: str, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.ImportFrom) and node.module == module and any(alias.name == name for alias in node.names)
        for node in ast.walk(tree)
    )


def _render_brief_section_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    render = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_brief"),
        None,
    )
    if render is None:
        return []
    calls = sorted(
        (node for node in ast.walk(render) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    return [node.func.id for node in calls if isinstance(node.func, ast.Name)]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_morning_brief_render_domain.py SRC_ROOT TEST_ROOT", file=sys.stderr)
        return 2

    src_root, test_root = map(Path, argv[1:])
    pursuit = src_root / "fieldkit/pursuit/projects.py"
    pursuit_command = src_root / "fieldkit/commands/pursuit/projects_health.py"
    watch = src_root / "fieldkit/watch/morning_brief_render.py"
    pursuit_io = src_root / "fieldkit/pursuit/io.py"
    old_render = src_root / "fieldkit/commands/watch/morning_brief_render.py"
    audit_command = src_root / "fieldkit/commands/pursuit/audit.py"
    companion_tests = test_root / "test_morning_brief_companion_pointer.py"
    classifier_tests = test_root / "test_projects_health_classify.py"
    quota_tests = test_root / "test_morning_brief.py"
    orchestration_tests = test_root / "test_morning_brief_inner.py"

    errors: list[str] = []
    for path in (
        pursuit,
        pursuit_command,
        pursuit_io,
        audit_command,
        watch,
        companion_tests,
        classifier_tests,
        quota_tests,
        orchestration_tests,
    ):
        if not path.is_file():
            errors.append(f"missing required file: {path}")
    if old_render.exists():
        errors.append(f"old command render module still exists: {old_render}")

    if not errors:
        missing_pursuit = PURSUIT_SYMBOLS - _definitions(pursuit)
        missing_watch = WATCH_SYMBOLS - _definitions(watch)
        stale_command_defs = PURSUIT_SYMBOLS & _definitions(pursuit_command)
        if missing_pursuit:
            errors.append(f"pursuit domain missing definitions: {sorted(missing_pursuit)}")
        if missing_watch:
            errors.append(f"watch domain missing definitions: {sorted(missing_watch)}")
        if stale_command_defs:
            errors.append(f"pursuit command still defines domain symbols: {sorted(stale_command_defs)}")
        command_lines = _command_imports(pursuit)
        if command_lines:
            errors.append(f"pursuit domain imports command modules at lines: {command_lines}")
        if "parse_frontmatter_fallback" not in _definitions(pursuit_io):
            errors.append("pursuit I/O does not define shared parse_frontmatter_fallback")
        if "_parse_frontmatter" in _definitions(audit_command):
            errors.append("audit command still defines the old frontmatter parser")
        for consumer in (pursuit, audit_command):
            if not _imports_from(consumer, "fieldkit.pursuit.io", "parse_frontmatter_fallback"):
                errors.append(f"shared frontmatter fallback not imported by: {consumer}")
        section_calls = _render_brief_section_calls(watch)
        expected_tail = ["_render_quota_section", "_render_project_health_section", "_render_companion_outbox_pointer"]
        indices = [section_calls.index(name) if name in section_calls else -1 for name in expected_tail]
        if -1 in indices or indices != sorted(indices):
            errors.append(
                "render_brief must retain quota, project-health, companion-pointer order; "
                f"observed calls: {section_calls}"
            )
        missing_companion_tests = COMPANION_TESTS - _definitions(companion_tests)
        if missing_companion_tests:
            errors.append(f"missing companion-pointer tests: {sorted(missing_companion_tests)}")
        if CLASSIFIER_TEST not in _definitions(classifier_tests):
            errors.append(f"missing empty-frontmatter classifier test: {CLASSIFIER_TEST}")
        missing_quota_tests = QUOTA_RENDER_TESTS - _definitions(quota_tests)
        if missing_quota_tests:
            errors.append(f"missing quota collector tests: {sorted(missing_quota_tests)}")
        if QUOTA_ORCHESTRATION_TEST not in _definitions(orchestration_tests):
            errors.append(f"missing quota orchestration test: {QUOTA_ORCHESTRATION_TEST}")

    for root in (src_root, test_root):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if OLD_RENDER_MODULE in text:
                errors.append(f"stale old render import or patch target: {path}")
            if OLD_CLASSIFIER_IMPORT in text or OLD_CLASSIFIER_PATCH in text:
                errors.append(f"stale project classifier import or patch target: {path}")

    outbox = src_root / "fieldkit/companion/outbox.py"
    if outbox.is_file() and "commands/watch/morning_brief_render.py" in outbox.read_text(encoding="utf-8"):
        errors.append(f"stale companion documentation pointer: {outbox}")
    generate = src_root / "fieldkit/commands/brief/generate.py"
    if generate.is_file() and OLD_RENDER_DOC in generate.read_text(encoding="utf-8"):
        errors.append(f"stale generator documentation pointer: {generate}")
    pursuit_agents = src_root / "fieldkit/pursuit/AGENTS.md"
    if pursuit_agents.is_file() and OLD_PARSER_GUIDANCE in pursuit_agents.read_text(encoding="utf-8"):
        errors.append(f"stale pursuit parser guidance: {pursuit_agents}")

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("OK: morning-brief render and project classification have domain ownership")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
