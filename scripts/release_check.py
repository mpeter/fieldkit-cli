"""Build one current release candidate and render fail-closed readiness evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import check_public_candidate, git_worktree, release_manual_evidence
from scripts._release_governance import load_policy
from scripts._release_governance import validate as validate_governance


@dataclass(frozen=True)
class Report:
    """Versioned readiness result for one immutable source revision."""

    schema_version: int
    status: str
    revision: str
    criteria: tuple[release_manual_evidence.Criterion, ...]
    evidence_ledger_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "revision": self.revision,
            "criteria": [
                {key: value for key, value in asdict(item).items() if value is not None} for item in self.criteria
            ],
            **(
                {"evidence_ledger_sha256": self.evidence_ledger_sha256}
                if self.evidence_ledger_sha256 is not None
                else {}
            ),
        }


def _governance(repo: Path, candidate_report: Path) -> tuple[str, tuple[str, ...]]:
    report = validate_governance(repo / release_manual_evidence.GOVERNANCE_POLICY, candidate_report)
    return report.status, report.pending_controls


def _write(path: Path, report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n").encode()
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manual-evidence", type=Path)
    parser.add_argument("--private-candidate-report", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.output.unlink(missing_ok=True)
        repo = args.repo.resolve()
        git_worktree.require_clean_worktree(repo)
        head = git_worktree.head_revision(repo)
        if args.revision != head:
            raise ValueError("requested revision is not the current checkout HEAD")
        candidate = load_policy(repo / release_manual_evidence.GOVERNANCE_POLICY).candidate
        check_public_candidate.check_candidate(
            repo,
            head,
            expected_repository=candidate.repository,
            planned_tag=candidate.planned_tag,
            output_dir=args.output_dir.resolve(),
        )
        governance_status, pending = _governance(repo, args.output_dir / "report.json")
        if governance_status not in {"pass", "pending"}:
            raise ValueError("release governance returned an unsupported status")
        evidence_ledger_sha256: str | None = None
        if args.manual_evidence is not None:
            external_criteria, evidence_ledger_sha256 = release_manual_evidence.validate_paths(
                repo,
                args.manual_evidence,
                args.output_dir / "report.json",
                candidate_bundle=args.output_dir / "bundle",
                private_candidate_report=args.private_candidate_report,
            )
            external_criteria = (
                *external_criteria,
                *(
                    release_manual_evidence.Criterion(control, "pending", "unrecognized pending governance control")
                    for control in pending
                    if control not in release_manual_evidence.EXTERNAL_CONTROLS
                ),
            )
        else:
            external_criteria = (
                *(
                    release_manual_evidence.Criterion(
                        control, "pending", "operator-recorded same-candidate external evidence"
                    )
                    for control in pending
                ),
                *(
                    release_manual_evidence.Criterion(
                        gate, "pending", "operator-recorded same-candidate release evidence"
                    )
                    for gate in release_manual_evidence.MANUAL_GATES
                ),
            )
        criteria = (
            release_manual_evidence.Criterion("source-revision", "pass", "current checkout HEAD"),
            release_manual_evidence.Criterion("public-candidate", "pass", "verified export and retained candidate"),
            *external_criteria,
        )
        report = Report(
            1,
            "pass" if all(criterion.status == "pass" for criterion in criteria) else "pending",
            head,
            criteria,
            evidence_ledger_sha256,
        )
        status = 0 if report.status == "pass" else 1
    except Exception as error:  # noqa: BLE001 - the release boundary must replace stale pass evidence
        report = Report(
            1, "failed", args.revision, (release_manual_evidence.Criterion("source-revision", "failed", str(error)),)
        )
        status = 2
    _write(args.output, report)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Release check: {report.status.upper()} ({args.output})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
