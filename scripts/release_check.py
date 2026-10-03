"""Build one current release candidate and render fail-closed readiness evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from scripts import check_public_candidate
    from scripts._release_governance import load_policy
    from scripts._release_governance import validate as validate_governance
else:
    import check_public_candidate
    from _release_governance import load_policy
    from _release_governance import validate as validate_governance


@dataclass(frozen=True)
class Criterion:
    """One release criterion with its authoritative evidence source."""

    id: str
    status: str
    evidence: str


@dataclass(frozen=True)
class Report:
    """Versioned readiness result for one immutable source revision."""

    schema_version: int
    status: str
    revision: str
    criteria: tuple[Criterion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "revision": self.revision,
            "criteria": [asdict(item) for item in self.criteria],
        }


def _head_revision(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise ValueError("repository HEAD is unavailable")
    revision = result.stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("repository HEAD is not a full lowercase commit SHA")
    return revision


def _require_clean_worktree(repo: Path) -> None:
    """Reject uncommitted policy or source changes from release evidence."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise ValueError("repository worktree status is unavailable")
    if result.stdout:
        raise ValueError("release check requires a clean worktree")


def _policy_path(repo: Path) -> Path:
    return repo / "docs/release-readiness/release-governance-policy.json"


def _write(path: Path, report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo = args.repo.resolve()
        _require_clean_worktree(repo)
        head = _head_revision(repo)
        if args.revision != head:
            raise ValueError("requested revision is not the current checkout HEAD")
        candidate = load_policy(_policy_path(repo)).candidate
        check_public_candidate.check_candidate(
            repo,
            head,
            expected_repository=candidate.repository,
            planned_tag=candidate.planned_tag,
            output_dir=args.output_dir.resolve(),
        )
        validate_governance(_policy_path(repo), args.output_dir / "report.json")
        report = Report(
            1,
            "pass",
            head,
            (
                Criterion("source-revision", "pass", "current checkout HEAD"),
                Criterion("public-candidate", "pass", "verified export and retained candidate"),
                Criterion("release-governance", "pass", "candidate report matches the governance policy"),
            ),
        )
        status = 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report = Report(1, "failed", args.revision, (Criterion("source-revision", "failed", str(error)),))
        status = 2
    _write(args.output, report)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Release check: {report.status.upper()} ({args.output})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
