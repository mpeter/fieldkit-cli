#!/usr/bin/env python3
"""Prove each installed-artifact command published in the public documentation."""

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from scripts import smoke_artifact
else:
    import smoke_artifact

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_TIMEOUT_SECONDS = 300
_BLOCK_CRITERIA = {
    "readme.md.block-2": ("SMOKE119", "SMOKE120", "SMOKE113", "SMOKE112", "SMOKE106"),
    "docs.getting.started.md.block-3": ("SMOKE119", "SMOKE120"),
    "docs.getting.started.md.block-5": ("SMOKE113", "SMOKE115"),
    "docs.getting.started.md.block-6": ("SMOKE113", "SMOKE115", "SMOKE112", "SMOKE106"),
    "docs.getting.started.md.block-7": ("SMOKE112", "SMOKE106"),
    "docs.getting.started.md.block-8": ("SMOKE116", "SMOKE117", "SMOKE118"),
    "docs.reference.config.file.md.block-2": ("SMOKE113", "SMOKE115"),
    "docs.reference.config.file.md.block-6": ("SMOKE112",),
    "docs.reference.troubleshooting.md.block-1": ("SMOKE101", "SMOKE105", "SMOKE112"),
    "docs.user.guide.md.block-1": ("SMOKE113", "SMOKE102", "SMOKE106"),
    "docs.guides.watchers.md.block-1": ("SMOKE121",),
    "docs.guides.watchers.md.block-2": ("SMOKE122",),
    "docs.guides.init.md.block-2": ("SMOKE123",),
    "docs.guides.init.md.block-3": ("SMOKE123",),
    "docs.guides.morning.brief.md.block-1": ("SMOKE112", "SMOKE124"),
    "docs.guides.morning.brief.md.block-3": ("SMOKE124",),
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
        ("uv", "build", "--out-dir", str(output_directory)),
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


def check(repo_root: Path = _REPO_ROOT) -> DocumentationScenarioEvidence:
    """Build fresh artifacts and fail unless each mapped command criterion passes twice."""
    declared_blocks = _safe_block_identifiers(repo_root)
    expected_blocks = set(_BLOCK_CRITERIA)
    if declared_blocks != expected_blocks:
        raise ValueError(
            "safe installed-artifact documentation blocks do not match fixed scenario map: "
            f"declared={sorted(declared_blocks)}, mapped={sorted(expected_blocks)}"
        )
    with tempfile.TemporaryDirectory(prefix="fieldkit-documentation-scenarios-") as temporary_directory:
        wheel, sdist = _build_artifacts(repo_root, Path(temporary_directory) / "dist")
        reports = (
            smoke_artifact.smoke(wheel, repo_root=repo_root),
            smoke_artifact.smoke(sdist, repo_root=repo_root),
        )
    observed = [{criterion.criterion_id: criterion.status for criterion in report.criteria} for report in reports]
    failures = tuple(
        f"{block}:{criterion_id}"
        for block, criterion_ids in _BLOCK_CRITERIA.items()
        for criterion_id in criterion_ids
        if any(criteria.get(criterion_id) != "pass" for criteria in observed)
    )
    return DocumentationScenarioEvidence(
        status="pass" if not failures else "fail",
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
    args = parser.parse_args(argv)
    try:
        evidence = check(args.repo_root.resolve())
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
