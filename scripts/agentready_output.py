"""Retain isolated assessor runs and publish only their fresh report pointer."""

import tempfile
from pathlib import Path

if __package__:
    from scripts import agentready_policy
else:
    import agentready_policy


def begin_run(output: Path) -> Path:
    """Archive the previous pointer without following it, then isolate output."""
    if output.is_symlink():
        raise ValueError("assessment output directory must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    latest = output / "assessment-latest.json"
    if latest.exists() and not latest.is_file() and not latest.is_symlink():
        raise ValueError("unexpected assessment pointer type")
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    if latest.is_symlink() or latest.is_file():
        latest.replace(output / f"{run.name}-previous.json")
    return run


def publish_run(run: Path, output: Path) -> None:
    """Publish only a nonempty bounded report confined to this invocation."""
    resolved_run = run.resolve(strict=True)
    if resolved_run.parent != output.resolve(strict=True) or run.is_symlink():
        raise ValueError("assessment run is outside its output directory")
    report = (run / "assessment-latest.json").resolve(strict=True)
    if not report.is_relative_to(resolved_run):
        raise ValueError("assessment report points outside its invocation")
    if not report.is_file():
        raise ValueError("assessment report must be a regular file")
    with report.open("rb") as stream:
        payload = stream.read(agentready_policy.MAX_REPORT_BYTES + 1)
    if not payload or len(payload) > agentready_policy.MAX_REPORT_BYTES:
        raise ValueError("assessment report is empty or exceeds its byte limit")
    (output / "assessment-latest.json").symlink_to(Path(run.name) / "assessment-latest.json")
