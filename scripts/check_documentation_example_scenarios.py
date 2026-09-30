#!/usr/bin/env python3
"""Prove each installed-artifact command published in the public documentation."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import smoke_artifact
from scripts.documentation_dependency_input import verify_dependencies

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_TIMEOUT_SECONDS = 300
_BLOCK_CRITERIA = {
    "docs.guides.gmail.md.block-11": ("SMOKE145", "SMOKE146", "SMOKE147", "SMOKE148"),
    "docs.guides.gmail.md.block-5": ("SMOKE130", "SMOKE141", "SMOKE142"),
    "docs.guides.gmail.md.block-7": ("SMOKE130", "SMOKE141", "SMOKE142", "SMOKE144"),
    "docs.guides.gmail.md.block-8": ("SMOKE130", "SMOKE141", "SMOKE143"),
    "readme.md.block-2": ("SMOKE119", "SMOKE120", "SMOKE158", "SMOKE153"),
    "docs.getting.started.md.block-1": ("SMOKE125", "SMOKE126"),
    "docs.getting.started.md.block-3": ("SMOKE119", "SMOKE120"),
    "docs.getting.started.md.block-5": ("SMOKE113", "SMOKE115"),
    "docs.getting.started.md.block-6": ("SMOKE155", "SMOKE151", "SMOKE152", "SMOKE153"),
    "docs.getting.started.md.block-7": ("SMOKE151", "SMOKE152", "SMOKE153"),
    "docs.getting.started.md.block-8": ("SMOKE119", "SMOKE120", "SMOKE154", "SMOKE149"),
    "docs.reference.config.file.md.block-2": ("SMOKE113", "SMOKE115"),
    "docs.reference.config.file.md.block-8": ("SMOKE112",),
    "docs.reference.troubleshooting.md.block-1": ("SMOKE101", "SMOKE105", "SMOKE112"),
    "docs.reference.troubleshooting.md.block-2": ("SMOKE101", "SMOKE125"),
    "docs.user.guide.md.block-1": ("SMOKE159", "SMOKE153"),
    "docs.user.guide.md.block-3": ("SMOKE160", "SMOKE153"),
    "docs.guides.watchers.md.block-1": ("SMOKE121",),
    "docs.guides.watchers.md.block-2": ("SMOKE122",),
    "docs.guides.watchers.md.block-6": ("SMOKE130", "SMOKE133", "SMOKE134", "SMOKE135", "SMOKE136", "SMOKE137"),
    "docs.reference.troubleshooting.md.block-5": ("SMOKE130", "SMOKE133", "SMOKE134", "SMOKE136", "SMOKE137"),
    "docs.reference.environment.vars.md.block-1": ("SMOKE130", "SMOKE138"),
    "docs.reference.environment.vars.md.block-2": ("SMOKE130", "SMOKE139", "SMOKE140"),
    "docs.guides.init.md.block-2": ("SMOKE123",),
    "docs.guides.init.md.block-1": ("SMOKE158", "SMOKE153"),
    "docs.guides.init.md.block-3": ("SMOKE123",),
    "docs.guides.morning.brief.md.block-1": ("SMOKE112",),
    "docs.guides.pipeline.workflow.md.block-7": ("SMOKE115", "SMOKE127", "SMOKE129"),
    "docs.guides.pipeline.workflow.md.block-3": ("SMOKE130", "SMOKE131"),
    "docs.guides.pipeline.workflow.md.block-5": ("SMOKE130", "SMOKE132"),
    "docs.guides.pipeline.workflow.md.block-9": ("SMOKE115", "SMOKE127", "SMOKE128"),
    "src.fieldkit.skills.meeting.skill.md.block-1": ("SMOKE156",),
    "src.fieldkit.skills.post.meeting.skill.md.block-1": ("SMOKE157",),
}


@dataclass(frozen=True)
class DocumentationScenarioEvidence:
    """Bounded artifact evidence linked to every safe public command block."""

    status: str
    scenarios: dict[str, tuple[str, ...]]
    failures: tuple[str, ...]
    artifacts: tuple[smoke_artifact.SmokeReport, ...]

    def to_dict(self) -> dict[str, object]:
        """Render stable, auditable machine-readable evidence."""
        return {
            "schema_version": 1,
            "status": self.status,
            "scenarios": {block: list(criteria) for block, criteria in self.scenarios.items()},
            "failures": list(self.failures),
            "artifacts": [report.to_dict() for report in self.artifacts],
        }

    def summary(self) -> dict[str, object]:
        """Render the bounded evidence retained by the parent documentation report."""
        return {
            "schema_version": 1,
            "status": self.status,
            "scenarios": {block: list(criteria) for block, criteria in self.scenarios.items()},
            "failures": list(self.failures),
            "artifacts": [
                {
                    "name": report.artifact_name,
                    "sha256": report.artifact_sha256,
                    "source_revision": report.source_revision,
                    **({"source_binding": "unattested-dirty"} if report.source_revision is None else {}),
                    **(
                        {
                            "criteria": [
                                {"criterion_id": criterion.criterion_id, "status": criterion.status}
                                for criterion in report.criteria
                            ]
                        }
                        if report.source_revision is None
                        else {}
                    ),
                }
                for report in self.artifacts
            ],
        }


def _safe_block_identifiers(repo_root: Path) -> set[str]:
    """Return exactly the contract blocks asserted by installed-artifact smoke scenarios."""
    contract = json.loads((repo_root / _CONTRACT_PATH).read_text(encoding="utf-8"))
    documents = contract.get("documents") if isinstance(contract, dict) else None
    if not isinstance(documents, dict):
        raise ValueError("documentation contract documents must be an object")
    identifiers: set[str] = set()
    for document in documents.values():
        if not isinstance(document, dict):
            continue
        for records in (document.get("fenced_blocks"), document.get("tables")):
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get("verification_id") == "automated.installed-base-artifact":
                    identifier = record.get("id")
                    if not isinstance(identifier, str):
                        raise ValueError("safe documentation command block needs a string identifier")
                    identifiers.add(identifier)
    return identifiers


def _build_artifacts(repo_root: Path, output_directory: Path) -> tuple[Path, Path]:
    """Build one fresh wheel and sdist pair without retaining disposable evidence."""
    result = subprocess.run(
        ("uv", "build", "--no-build-isolation", "--out-dir", str(output_directory)),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"artifact build failed: {detail[-1000:]}")
    wheels = tuple(output_directory.glob("fieldkit_cli-*.whl"))
    sdists = tuple(output_directory.glob("fieldkit_cli-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("artifact build did not produce exactly one wheel and one sdist")
    return wheels[0], sdists[0]


def check(
    repo_root: Path = _REPO_ROOT,
    *,
    source_revision: str | None = None,
    diagnostic_dirty: bool = False,
    dependency_root: Path | None = None,
    dependency_manifest_sha256: str | None = None,
) -> DocumentationScenarioEvidence:
    """Require both complete artifact smokes and every mapped documentation criterion."""
    if source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("snapshot source revision must be a full lowercase commit identity")
    if diagnostic_dirty and source_revision is not None:
        raise ValueError("dirty diagnostic artifacts cannot claim a source revision")
    if dependency_root is None or dependency_manifest_sha256 is None:
        raise ValueError("offline dependency root and externally retained manifest digest are required")
    declared_blocks = _safe_block_identifiers(repo_root)
    expected_blocks = set(_BLOCK_CRITERIA)
    if declared_blocks != expected_blocks:
        raise ValueError(
            "safe installed-artifact documentation blocks do not match fixed scenario map: "
            f"declared={sorted(declared_blocks)}, mapped={sorted(expected_blocks)}"
        )
    with (
        verify_dependencies(dependency_root, dependency_manifest_sha256, repo_root) as dependencies,
        tempfile.TemporaryDirectory(prefix="fieldkit-documentation-scenarios-") as temporary_directory,
    ):
        wheel, sdist = _build_artifacts(repo_root, Path(temporary_directory) / "dist")

        def run_artifact(artifact: Path) -> smoke_artifact.SmokeReport:
            if diagnostic_dirty:
                return smoke_artifact.smoke(
                    artifact,
                    repo_root=repo_root,
                    diagnostic_dirty=True,
                    offline_dependencies=dependencies.for_artifact(artifact),
                )
            return smoke_artifact.smoke(
                artifact,
                repo_root=repo_root,
                source_revision=source_revision,
                offline_dependencies=dependencies.for_artifact(artifact),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            wheel_result = executor.submit(run_artifact, wheel)
            sdist_result = executor.submit(run_artifact, sdist)
            reports = (wheel_result.result(), sdist_result.result())
    if diagnostic_dirty and any(report.source_revision is not None for report in reports):
        raise ValueError("dirty diagnostic artifacts cannot claim a source revision")
    observed = [{criterion.criterion_id: criterion.status for criterion in report.criteria} for report in reports]
    failures = tuple(
        f"{report.artifact_name}:{criterion.criterion_id}"
        for report in reports
        for criterion in report.criteria
        if criterion.status != "pass"
    ) + tuple(
        f"{block}:{criterion_id}"
        for block, criterion_ids in _BLOCK_CRITERIA.items()
        for criterion_id in criterion_ids
        if any(criteria.get(criterion_id) != "pass" for criteria in observed)
    )
    return DocumentationScenarioEvidence(
        status="fail" if failures else "diagnostic" if any(not report.ok for report in reports) else "pass",
        scenarios=_BLOCK_CRITERIA,
        failures=failures,
        artifacts=reports,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the fixed documentation scenarios and optionally write their evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--diagnostic-dirty", action="store_true")
    parser.add_argument("--dependency-root", type=Path, required=True)
    parser.add_argument("--dependency-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = check(
            args.repo_root.resolve(),
            source_revision=os.environ.get("FIELDKIT_DOC_SOURCE_REVISION"),
            diagnostic_dirty=args.diagnostic_dirty,
            dependency_root=args.dependency_root,
            dependency_manifest_sha256=args.dependency_manifest_sha256,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Documentation artifact scenarios: ERROR: {error}", file=sys.stderr)
        return 2
    if args.report is not None:
        args.report.write_text(json.dumps(evidence.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.as_json:
        print(json.dumps(evidence.summary(), indent=2, sort_keys=True))
    else:
        print(f"Documentation artifact scenarios: {evidence.status.upper()} ({len(evidence.scenarios)} mapped blocks)")
    return 0 if evidence.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
