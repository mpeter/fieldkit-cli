"""fieldkit.companion.runner — the single execution choke point (design D4/D5).

``run_action`` does gate-check, subprocess-execute, and journal-append
in one call. The bundled skill instructs the agent to never invoke
commands directly — everything goes through here so the journal is a
complete record of what the sidecar did.
"""

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fieldkit.companion.gate import ValidatedActPolicy, is_allowed, matches_configured_authority
from fieldkit.companion.journal import append_journal
from fieldkit.config import TIMEOUT_COMPANION_ACTION

# Exit code for gate denial: EXIT_DATA. Denial is permanent, not transient —
# exit 1 ("retry may help") would send an orchestrator into a retry loop.
EXIT_DENIED = 3


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one gated action."""

    argv: list[str]
    exit_code: int
    denied: bool
    stdout: str
    stderr: str


def run_action(
    argv: list[str],
    *,
    tier: str,
    policy: ValidatedActPolicy,
    data_path: Path,
    item_id: str = "",
    journal: bool = True,
) -> ActionResult:
    """Gate-check *argv*, execute it as a fieldkit subprocess, journal the outcome.

    Args:
        argv: fieldkit command tokens (no program name).
        tier: The configured companion tier.
        policy: Canonically validated act-tier command permissions.
        data_path: Data root for the journal.
        item_id: The attention item this action addresses (may be empty
            for ad-hoc actions; still journaled).
        journal: When True (default), append the outcome to the journal here.
            A caller that must order the journal append after another durable
            side effect (the loop writing its proposal first, design C2) passes
            False and journals the outcome itself.

    Returns:
        ActionResult. Denied actions never spawn a subprocess and are
        journaled with ``EXIT_DENIED`` so the audit trail shows refusals too.
    """
    action = shlex.join(argv)
    if not matches_configured_authority(tier, policy):
        return ActionResult(
            argv=argv,
            exit_code=EXIT_DENIED,
            denied=True,
            stdout="",
            stderr="invalid companion action authority",
        )
    if not is_allowed(argv, tier, policy):
        if journal:
            append_journal(data_path, item_id=item_id, action=action, exit_code=EXIT_DENIED)
        return ActionResult(argv=argv, exit_code=EXIT_DENIED, denied=True, stdout="", stderr="")

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "fieldkit", *argv],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_COMPANION_ACTION,
            check=False,  # the exit code is the result, not an error
        )
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 1, "", f"timed out after {TIMEOUT_COMPANION_ACTION}s"

    if journal:
        append_journal(data_path, item_id=item_id, action=action, exit_code=exit_code)
    return ActionResult(argv=argv, exit_code=exit_code, denied=False, stdout=stdout, stderr=stderr)
